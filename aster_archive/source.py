"""Bounded, streaming readers for immutable ChatGPT export snapshots.

The content-addressed object is the original file, not reserialized JSON. ZIP
members are read in place; this module never extracts paths supplied by a ZIP.
Inventory CRC values are declarations, not a claim that every asset was read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import BinaryIO, Iterator
import zipfile

import ijson


class SourceError(ValueError):
    """The snapshot cannot be safely or unambiguously imported."""


@dataclass(frozen=True)
class SourceLimits:
    max_archive_bytes: int = 32 * 1024**3
    max_uncompressed_bytes: int = 64 * 1024**3
    max_member_bytes: int = 16 * 1024**3
    max_members: int = 100_000
    max_compression_ratio: int = 1_000
    max_conversation_bytes: int = 64 * 1024**2

    def __post_init__(self) -> None:
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("source limits must be positive")


@dataclass(frozen=True)
class SourceArchive:
    sha256: str
    size: int
    path: Path
    kind: str
    inventory: tuple[dict, ...] = ()
    limits: SourceLimits = field(default_factory=SourceLimits)


_SHARD = re.compile(r"^conversations-(\d+)\.json$")
_TOKENS = re.compile(rb'["\\{}\[\],]')
_CHUNK = 64 * 1024


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _fsync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if "\x00" in info.orig_filename or "\\" in name or not name:
        raise SourceError("unsafe ZIP member name")
    stripped = name[:-1] if name.endswith("/") else name
    parts = stripped.split("/")
    if name.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise SourceError("unsafe ZIP member path")
    if ":" in parts[0]:
        raise SourceError("rooted ZIP member path")
    mode = stat.S_IFMT(info.external_attr >> 16)
    if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise SourceError("ZIP links and special files are unsupported")
    if info.flag_bits & 1:
        raise SourceError("encrypted ZIP members are unsupported")
    return stripped


def _conversation_members(inventory: tuple[dict, ...]) -> list[str]:
    candidates: list[tuple[str, int | None, str]] = []
    for item in inventory:
        if item["directory"]:
            continue
        path = PurePosixPath(item["name"])
        if path.name == "conversations.json":
            candidates.append((str(path.parent), None, item["name"]))
        elif match := _SHARD.fullmatch(path.name):
            if len(match.group(1)) > 12:
                raise SourceError("conversation shard number is invalid")
            candidates.append((str(path.parent), int(match.group(1)), item["name"]))
    if not candidates:
        raise SourceError("no supported conversations.json or conversation shards")
    if len({parent for parent, _, _ in candidates}) != 1:
        raise SourceError("multiple conversation export directories are ambiguous")
    single = [name for _, index, name in candidates if index is None]
    if single:
        if len(candidates) != 1:
            raise SourceError("mixed monolithic and sharded conversation files")
        return single
    shards = sorted((index, name) for _, index, name in candidates)
    if [index for index, _ in shards] != list(range(len(shards))):
        raise SourceError("conversation shard numbering has duplicates or gaps")
    return [name for _, name in shards]


def _inspect(path: Path, original_name: str, limits: SourceLimits) -> tuple[str, tuple[dict, ...]]:
    with path.open("rb") as stream:
        prefix = stream.read(4)
    if prefix.startswith(b"PK") or zipfile.is_zipfile(path):
        inventory = []
        names: set[str] = set()
        total = 0
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > limits.max_members:
                    raise SourceError("ZIP member count exceeds configured limit")
                for info in members:
                    name = _member_name(info)
                    if name in names:
                        raise SourceError("duplicate ZIP member path")
                    names.add(name)
                    total += info.file_size
                    if info.file_size > limits.max_member_bytes or total > limits.max_uncompressed_bytes:
                        raise SourceError("ZIP uncompressed size exceeds configured limit")
                    if info.file_size > limits.max_compression_ratio * max(1, info.compress_size):
                        raise SourceError("ZIP compression ratio exceeds configured limit")
                    inventory.append({
                        "name": info.filename, "size": info.file_size,
                        "compressed_size": info.compress_size, "crc": info.CRC,
                        "directory": info.is_dir(), "crc_verified": False,
                    })
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
            raise SourceError("invalid or unsupported ZIP container") from error
        result = tuple(inventory)
        _conversation_members(result)
        return "zip", result
    # A bare JSON array is supported. The streaming parser checks all content.
    with path.open("rb") as stream:
        start = stream.read(_CHUNK)
    if start.startswith(b"\xef\xbb\xbf"):
        start = start[3:]
    if not start.lstrip(b" \r\n\t").startswith(b"["):
        raise SourceError("unsupported export format: expected ZIP or JSON array")
    size = path.stat().st_size
    if size > limits.max_member_bytes:
        raise SourceError("JSON member exceeds configured limit")
    # Canonical bare-member identity survives a resume from its hash-only CAS
    # filename. The caller can retain the acquisition filename separately.
    return "json", ({"name": "conversations.json", "size": size, "compressed_size": size,
                      "crc": None, "directory": False, "crc_verified": None},)


def prepare_source(path: Path, archive_root: Path, limits: SourceLimits | None = None) -> SourceArchive:
    """Copy a complete input into CAS and return its verified byte identity.

    Existing objects must already have the expected hash and size. A corrupt
    object is never overwritten. A failed/incomplete input is not published.
    Input files changing during the copy are rejected; callers should finalize
    a download by atomically renaming it into their import directory.
    """
    limits = limits or SourceLimits()
    path = Path(path)
    archive_root = Path(archive_root)
    if path.is_symlink():
        raise SourceError("source must be a regular file, not a link")
    archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    temp_path: Path | None = None
    try:
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SourceError("source must be a regular file")
            if before.st_size > limits.max_archive_bytes:
                raise SourceError("source size exceeds configured limit")
            digest = hashlib.sha256()
            size = 0
            with tempfile.NamedTemporaryFile(dir=archive_root, prefix=".pending-", delete=False) as target:
                temp_path = Path(target.name)
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > limits.max_archive_bytes:
                        raise SourceError("source size exceeds configured limit")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != after.st_size:
                raise SourceError("source changed while being copied")
        kind, inventory = _inspect(temp_path, path.name, limits)
        sha256 = digest.hexdigest()
        parent = archive_root / "objects" / "sha256" / sha256[:2]
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = parent / sha256
        try:
            # link is an atomic, exclusive publication on the same filesystem.
            os.chmod(temp_path, 0o400)
            os.link(temp_path, destination)
            # Persist new directory entries all the way back to the CAS root;
            # syncing only the leaf would not persist newly created ancestors.
            for directory in (parent, parent.parent, parent.parent.parent, archive_root):
                _fsync_directory(directory)
        except FileExistsError:
            if (destination.is_symlink() or not destination.is_file()
                    or destination.stat().st_size != size or _hash_file(destination) != (sha256, size)):
                raise SourceError("existing content-addressed object is corrupt") from None
        return SourceArchive(sha256, size, destination, kind, inventory, limits)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_source(path: Path, expected_sha256: str, limits: SourceLimits | None = None) -> SourceArchive:
    """Verify and reopen an existing CAS snapshot without copying its bytes.

    The caller must supply a path within its protected archive directory. File
    identity changes during hashing/inspection are rejected. As with the CAS
    writer, the containing directory must remain protected from other writers.
    """
    limits = limits or SourceLimits()
    path = Path(path)
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SourceError("expected source SHA256 is invalid")
    if path.is_symlink():
        raise SourceError("source must be a regular file, not a link")

    def identity(value: os.stat_result) -> tuple:
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise SourceError("source must be a regular file")
        if before.st_size > limits.max_archive_bytes:
            raise SourceError("source size exceeds configured limit")
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > limits.max_archive_bytes:
                raise SourceError("source size exceeds configured limit")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if identity(before) != identity(after) or size != before.st_size:
        raise SourceError("source changed while being verified")
    if digest.hexdigest() != expected_sha256:
        raise SourceError("source SHA256 does not match expected identity")
    if identity(path.stat(follow_symlinks=False)) != identity(before):
        raise SourceError("source changed while being verified")
    kind, inventory = _inspect(path, "conversations.json", limits)
    if identity(path.stat(follow_symlinks=False)) != identity(before):
        raise SourceError("source changed while being inspected")
    return SourceArchive(expected_sha256, size, path, kind, inventory, limits)


class _UniqueObject(dict):
    def __setitem__(self, key, value):
        if key in self:
            # The C ijson backend does not safely propagate exceptions raised
            # by map_type.__setitem__. Mark the bounded record and reject it
            # before yielding it instead of raising inside that callback.
            self.duplicate_key = True
        super().__setitem__(key, value)


def _duplicate_keys(value) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if getattr(item, "duplicate_key", False):
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


class _BoundedArrayReader:
    """Bound each raw array item before the JSON parser can buffer it fully.

    A small lexical guard tracks braces/quotes across read boundaries. The JSON
    parser remains responsible for grammar and Unicode validation. This avoids
    building an arbitrarily large conversation before applying the size limit.
    """

    def __init__(self, stream: BinaryIO, maximum: int):
        self.stream = stream
        self.maximum = maximum
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.active = False
        self.count = 0
        self.started = False
        self.finished = False
        self.first_read = True

    def _add(self, amount: int) -> None:
        if self.active:
            self.count += amount
            if self.count > self.maximum:
                raise SourceError("conversation record exceeds configured limit")

    def _span(self, data: bytes) -> None:
        if not data:
            return
        if self.in_string:
            self._add(len(data))
            # Any escaped nonstructural character consumes the pending escape.
            self.escaped = False
            return
        if self.depth == 1 and not self.active:
            stripped = data.lstrip(b" \t\r\n")
            if stripped:
                self.active = True
                self.count = 0
                self._add(len(stripped))
        else:
            self._add(len(data))

    def _scan(self, data: bytes) -> None:
        last = 0
        for match in _TOKENS.finditer(data):
            self._span(data[last:match.start()])
            char = match.group()[0]
            last = match.end()
            if self.in_string:
                self._add(1)
                if self.escaped:
                    self.escaped = False
                elif char == 92:
                    self.escaped = True
                elif char == 34:
                    self.in_string = False
                continue
            if not self.started:
                if char != 91:
                    raise SourceError("conversation file must contain a JSON array")
                self.started = True
                self.depth = 1
                continue
            if self.depth == 1 and char in (44, 93):
                self.active = False
                self.count = 0
                if char == 93:
                    self.depth = 0
                    self.finished = True
                continue
            if self.depth == 1 and not self.active:
                self.active = True
                self.count = 0
            self._add(1)
            if char == 34:
                self.in_string = True
            elif char in (91, 123):
                self.depth += 1
            elif char in (93, 125):
                self.depth -= 1
        self._span(data[last:])

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        data = self.stream.read(_CHUNK if size < 0 else min(size, _CHUNK))
        if self.first_read:
            self.first_read = False
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
        self._scan(data)
        return data


def _read_array(stream: BinaryIO, limits: SourceLimits) -> Iterator[tuple[int, dict]]:
    reader = _BoundedArrayReader(stream, limits.max_conversation_bytes)
    try:
        # JSON numbers use the same int/float representation as json.loads;
        # original number spellings and all other bytes remain in the CAS file.
        for ordinal, raw in enumerate(ijson.items(reader, "item", use_float=True, map_type=_UniqueObject)):
            if not isinstance(raw, dict):
                raise SourceError("conversation array item is not an object")
            if _duplicate_keys(raw):
                raise SourceError("duplicate object key in conversation JSON")
            yield ordinal, raw
        if not reader.started or not reader.finished:
            raise SourceError("conversation file is not a complete JSON array")
    except (ijson.JSONError, UnicodeError, OverflowError) as error:
        raise SourceError("invalid conversation JSON") from error


def iter_conversations(source: SourceArchive) -> Iterator[tuple[str, int, dict]]:
    """Stream one conversation at a time; consume to EOF to verify parse/CRC.

    This method does not claim CRC verification of unrelated attachment assets.
    A caller must only mark an import complete after this iterator finishes.
    """
    if source.kind == "json":
        member = source.inventory[0]["name"] if source.inventory else "conversations.json"
        with source.path.open("rb") as stream:
            for ordinal, raw in _read_array(stream, source.limits):
                yield member, ordinal, raw
    elif source.kind == "zip":
        try:
            with zipfile.ZipFile(source.path) as archive:
                for member in _conversation_members(source.inventory):
                    with archive.open(member) as stream:
                        for ordinal, raw in _read_array(stream, source.limits):
                            yield member, ordinal, raw
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError) as error:
            raise SourceError("corrupt or unsupported conversation ZIP member") from error
    else:
        raise SourceError("unsupported source kind")
