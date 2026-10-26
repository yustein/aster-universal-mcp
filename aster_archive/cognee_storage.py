"""Lossless storage for completed, immutable Cognee revision stores.

The archive reader uses PostgreSQL summaries and exact source passages. The
private Cognee graph/vector databases remain recoverable, with the same dataset
isolation, in a verified archive after every eligible passage has completed.
Receipts stay outside that archive so a database acknowledgement can replay
without reopening or extracting a graph. No model or corpus data is discarded.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import time

from .storage import ArchiveStorageError, require_storage_reserve

ARCHIVE_NAME = 'completed-storage.tar.xz'
MARKER_NAME = 'completed-storage.json'
STAGE_NAME = '.completed-storage-restore'
WRITER_BLOCK_NAME = '.writer-quiescence-blocked.json'
ROOTS = ('data', 'system', 'cache')
MAX_MEMBERS = 1_000_000
MAX_LOGICAL_BYTES = 128 * 1024**3
MAX_FILE_BYTES = 4 * 1024**3
MAX_MANIFEST_BYTES = 256 * 1024**2
MAX_LZMA_MEMORY_BYTES = 64 * 1024**2
CHUNK_BYTES = 1024**2


class CogneeStorageError(ArchiveStorageError):
    """Fail closed and retry outside passage-attempt accounting."""


def _fail(message):
    raise CogneeStorageError(message)


def _root(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        _fail('absolute non-symlink Cognee revision root required')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.resolve() != path:
        _fail('Cognee revision root must not traverse a symlink')
    return path


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def revision_lock(root):
    """Serialize compaction, hydration and the entire child process lifetime."""
    root = _root(root)
    fd = os.open(root / '.completed-storage.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _fail('invalid Cognee storage lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        if (root / WRITER_BLOCK_NAME).exists() or (root / WRITER_BLOCK_NAME).is_symlink():
            _fail('Cognee writer quiescence requires operator verification')
        yield root
    finally:
        os.close(fd)


def block_unquiescent_writer(root, pid):
    """Persist a fail-closed barrier if a child group could still mutate files.

    An operator must stop/reap the writer cgroup and verify quiescence before
    removing this barrier. Never infer safe deletion from the parent's exit.
    """
    root = _root(root)
    temporary = root / (WRITER_BLOCK_NAME + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump({'version': 1, 'reason': 'child_group_not_quiescent',
                   'pid_in_writer_namespace': pid, 'observed_at_unix': time.time()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / WRITER_BLOCK_NAME)
    _fsync_directory(root)


def _safe_name(name):
    if not isinstance(name, str) or not name or len(name.encode()) > 1024 or '\\' in name:
        _fail('invalid archived path')
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or '..' in path.parts or path.parts[0] not in ROOTS:
        _fail('archived path leaves its revision store')
    return path


def _hash_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_LOGICAL_BYTES:
            _fail('invalid archive storage file')
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(CHUNK_BYTES), b''):
            digest.update(block)
        after = os.fstat(handle.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            _fail('archive storage changed while reading')
        return digest.hexdigest()


def _inventory(root):
    entries = []
    total = 0

    def visit(path):
        nonlocal total
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        _safe_name(name)
        item = {'path': name, 'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns}
        if stat.S_ISDIR(info.st_mode):
            item.update(type='directory', size=0)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if info.st_size > MAX_FILE_BYTES:
                _fail('Cognee storage member exceeds its bound')
            total += info.st_size
            item.update(type='file', size=info.st_size, sha256=_hash_file(path))
        else:
            _fail('Cognee storage contains a symlink, hardlink or special file')
        entries.append(item)
        if len(entries) > MAX_MEMBERS or total > MAX_LOGICAL_BYTES:
            _fail('Cognee revision storage exceeds its archive bound')
        if item['type'] == 'directory':
            for child in sorted(path.iterdir()):
                visit(child)

    for name in ROOTS:
        path = root / name
        if path.exists() or path.is_symlink():
            visit(path)
    return entries, total


def _validate_manifest(root, value):
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('state') not in ('frozen', 'hydrated')
            or value.get('scope_id') != root.name
            or value.get('archive') != ARCHIVE_NAME):
        _fail('invalid Cognee storage manifest')
    digest = value.get('archive_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        _fail('invalid Cognee archive hash')
    entries = value.get('entries')
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_MEMBERS:
        _fail('invalid Cognee storage inventory')
    known = {}
    total = 0
    files = 0
    for entry in entries:
        if not isinstance(entry, dict):
            _fail('invalid Cognee storage member')
        name = entry.get('path')
        path = _safe_name(name)
        if name in known or entry.get('type') not in ('directory', 'file'):
            _fail('duplicate or invalid Cognee storage member')
        if len(path.parts) > 1 and known.get(path.parent.as_posix(), {}).get('type') != 'directory':
            _fail('Cognee storage member has no directory parent')
        if type(entry.get('mode')) is not int or not 0 <= entry['mode'] <= 0o777:
            _fail('invalid Cognee storage member mode')
        if type(entry.get('mtime_ns')) is not int or entry['mtime_ns'] < 0:
            _fail('invalid Cognee storage member timestamp')
        if type(entry.get('size')) is not int or not 0 <= entry['size'] <= MAX_FILE_BYTES:
            _fail('invalid Cognee storage member size')
        if entry['type'] == 'directory' and entry['size'] != 0:
            _fail('invalid Cognee archive directory')
        if entry['type'] == 'file':
            digest = entry.get('sha256')
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                _fail('invalid Cognee storage member hash')
            total += entry['size']
            files += 1
        known[name] = entry
    if (total > MAX_LOGICAL_BYTES or total != value.get('logical_bytes')
            or files != value.get('file_count')):
        _fail('Cognee archive inventory totals do not match')
    return value


def _read_marker(root):
    path = root / MARKER_NAME
    if not path.exists() and not path.is_symlink():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
            _fail('Cognee storage manifest exceeds its bound')
        try:
            value = json.load(handle)
        except (ValueError, UnicodeError):
            _fail('invalid Cognee storage manifest JSON')
    return _validate_manifest(root, value)


def _write_marker(root, manifest):
    encoded = json.dumps(manifest, ensure_ascii=True, separators=(',', ':')).encode()
    if len(encoded) > MAX_MANIFEST_BYTES:
        _fail('Cognee storage manifest exceeds its bound')
    temporary = root / (MARKER_NAME + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / MARKER_NAME)
    _fsync_directory(root)


class _BoundedXZReader:
    """XZ decoding with explicit dictionary-memory and expanded-stream bounds."""
    def __init__(self, source, maximum):
        self.source = source
        self.decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=MAX_LZMA_MEMORY_BYTES)
        self.maximum = maximum
        self.expanded = 0
        self.finished = False

    def read(self, size):
        if type(size) is not int or size < 0:
            _fail('unbounded Cognee archive read requested')
        result = bytearray()
        while len(result) < size and not self.finished:
            if self.decoder.eof:
                if self.decoder.unused_data or self.source.read(1):
                    _fail('trailing data after Cognee XZ archive')
                self.finished = True
                break
            block = self.source.read(CHUNK_BYTES) if self.decoder.needs_input else b''
            if not block and self.decoder.needs_input:
                _fail('truncated Cognee XZ archive')
            decoded = self.decoder.decompress(block, max_length=size - len(result))
            self.expanded += len(decoded)
            if self.expanded > self.maximum:
                _fail('Cognee archive expanded stream exceeds its bound')
            result.extend(decoded)
        return bytes(result)

    def finish(self):
        while self.read(CHUNK_BYTES):
            pass


def _verify_archive(root, manifest, destination=None, *, archive_path=None):
    archive = archive_path or root / ARCHIVE_NAME
    if _hash_file(archive) != manifest['archive_sha256']:
        _fail('Cognee archive checksum mismatch')
    expected = {entry['path']: entry for entry in manifest['entries']}
    seen = set()
    try:
        fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            decoded = _BoundedXZReader(handle, manifest['logical_bytes'] + len(expected) * 4096 + 10240)
            source = tarfile.open(fileobj=decoded, mode='r|')
            for member in source:
                source.members.clear()  # Streaming mode otherwise retains every header.
                _safe_name(member.name)
                entry = expected.get(member.name)
                if entry is None or member.name in seen or len(seen) >= MAX_MEMBERS:
                    _fail('unexpected or duplicate Cognee archive member')
                if (not member.isfile() and not member.isdir()) or member.issym() or member.islnk():
                    _fail('Cognee archive contains a link or special member')
                if (member.size != entry['size'] or stat.S_IMODE(member.mode) != entry['mode']
                        or member.isdir() != (entry['type'] == 'directory')):
                    _fail('Cognee archive member metadata mismatch')
                seen.add(member.name)
                target = destination / member.name if destination is not None else None
                if member.isdir():
                    if target is not None:
                        target.mkdir(mode=0o700)
                    continue
                digest = hashlib.sha256()
                remaining = member.size
                reader = source.extractfile(member)
                if reader is None:
                    _fail('Cognee archive member cannot be read')
                output = None
                try:
                    if target is not None:
                        output = os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb')
                    while remaining:
                        block = reader.read(min(CHUNK_BYTES, remaining))
                        if not block:
                            _fail('truncated Cognee archive member')
                        digest.update(block)
                        remaining -= len(block)
                        if output is not None:
                            output.write(block)
                    if digest.hexdigest() != entry['sha256']:
                        _fail('Cognee archive member checksum mismatch')
                    if output is not None:
                        output.flush()
                        os.fchmod(output.fileno(), entry['mode'])
                        os.fsync(output.fileno())
                finally:
                    reader.close()
                    if output is not None:
                        output.close()
                if target is not None:
                    os.utime(target, ns=(entry['mtime_ns'], entry['mtime_ns']), follow_symlinks=False)
            source.close()
            decoded.finish()
    except (tarfile.TarError, EOFError, lzma.LZMAError) as error:
        raise CogneeStorageError('invalid or truncated Cognee archive') from error
    if seen != set(expected):
        _fail('Cognee archive inventory is incomplete')
    if destination is not None:
        for entry in reversed(manifest['entries']):
            if entry['type'] == 'directory':
                target = destination / entry['path']
                os.chmod(target, entry['mode'], follow_symlinks=False)
                os.utime(target, ns=(entry['mtime_ns'], entry['mtime_ns']), follow_symlinks=False)
                _fsync_directory(target)
        _fsync_directory(destination)


def _remove_live(root, manifest):
    # A committed frozen archive is authoritative, but never discard a new or
    # changed file left by an unexpected writer. Partial deletion is resumable.
    expected = {entry['path']: entry for entry in manifest['entries']}
    existing, _ = _inventory(root)
    for entry in existing:
        prior = expected.get(entry['path'])
        if prior is None or any(entry.get(key) != prior.get(key) for key in ('type', 'size', 'sha256', 'mode')):
            _fail('live Cognee store diverged from its frozen archive')
    for name in ROOTS:
        path = root / name
        if path.exists():
            if not path.is_dir() or path.is_symlink():
                _fail('invalid live Cognee storage directory')
            shutil.rmtree(path)
    _fsync_directory(root)


def _result(root, manifest, **extra):
    return {'status': manifest['state'], 'scope_id': root.name,
            'logical_bytes': manifest['logical_bytes'], 'file_count': manifest['file_count'],
            'archive_bytes': (root / ARCHIVE_NAME).stat().st_size,
            'archive_sha256': manifest['archive_sha256'], **extra}


def compact_revision(root, *, eligible_passages, completed_passages):
    """Freeze a completed revision; the caller proves completeness in PostgreSQL.

    The worker additionally holds its owner/source import lock while making the
    eligibility query and calling this function, after every child has closed.
    This function's lock excludes a child or manual hydrate in the same root.
    """
    if (type(eligible_passages) is not int or eligible_passages <= 0
            or type(completed_passages) is not int or eligible_passages != completed_passages):
        _fail('all eligible Cognee revision passages must be complete')
    with revision_lock(root) as root:
        current = _read_marker(root)
        if current is not None and current['state'] == 'frozen':
            _verify_archive(root, current)
            _remove_live(root, current)
            return _result(root, current, reused_archive=True)
        entries, total = _inventory(root)
        if not entries:
            return {'status': 'no_storage', 'scope_id': root.name}
        # Compression can expand unusual inputs slightly. Budget its worst
        # practical size before creating anything or removing source files.
        require_storage_reserve(root, additional_bytes=total + len(entries) * 2048 + CHUNK_BYTES)
        temporary = root / (ARCHIVE_NAME + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            with lzma.LZMAFile(handle, mode='wb', format=lzma.FORMAT_XZ, preset=3) as compressed:
                with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                    for entry in entries:
                        member = tarfile.TarInfo(entry['path'])
                        member.mode = entry['mode']
                        member.mtime = entry['mtime_ns'] // 10**9
                        member.type = tarfile.DIRTYPE if entry['type'] == 'directory' else tarfile.REGTYPE
                        member.size = entry['size']
                        if entry['type'] == 'directory':
                            archive.addfile(member)
                        else:
                            source_fd = os.open(root / entry['path'], os.O_RDONLY | os.O_NOFOLLOW)
                            with os.fdopen(source_fd, 'rb') as source:
                                archive.addfile(member, source)
                        archive.members.clear()
            handle.flush()
            os.fsync(handle.fileno())
        manifest = {'version': 1, 'state': 'frozen', 'scope_id': root.name,
                    'archive': ARCHIVE_NAME, 'archive_sha256': _hash_file(temporary),
                    'created_at_unix': time.time(), 'logical_bytes': total,
                    'file_count': sum(entry['type'] == 'file' for entry in entries),
                    'eligible_passages': eligible_passages, 'completed_passages': completed_passages,
                    'entries': entries}
        _validate_manifest(root, manifest)
        _verify_archive(root, manifest, archive_path=temporary)
        os.replace(temporary, root / ARCHIVE_NAME)
        _fsync_directory(root)
        # The durable marker commits the archive BEFORE deleting any original.
        _write_marker(root, manifest)
        _remove_live(root, manifest)
        return _result(root, manifest, reused_archive=False)


def _hydrate_locked(root):
    manifest = _read_marker(root)
    if manifest is None:
        return {'status': 'unpacked', 'scope_id': root.name}
    if manifest['state'] == 'hydrated':
        return _result(root, manifest)
    stage = root / STAGE_NAME
    if stage.exists() or stage.is_symlink():
        if not stage.is_dir() or stage.is_symlink():
            _fail('invalid Cognee restore staging directory')
        _verify_archive(root, manifest)
        # Only our fixed private staging directory is replaced after a crash.
        shutil.rmtree(stage)
    require_storage_reserve(root, additional_bytes=manifest['logical_bytes'] + len(manifest['entries']) * 4096)
    stage.mkdir(mode=0o700)
    _verify_archive(root, manifest, destination=stage)
    _remove_live(root, manifest)
    for name in ROOTS:
        if (stage / name).exists():
            os.replace(stage / name, root / name)
    stage.rmdir()
    _fsync_directory(root)
    # Until this atomic commit, an interrupted multi-directory promotion is
    # still frozen and must be rebuilt from the same verified archive.
    hydrated = manifest | {'state': 'hydrated', 'hydrated_at_unix': time.time()}
    _write_marker(root, hydrated)
    return _result(root, hydrated)


def hydrate_revision(root):
    """Restore the exact files before running an older worker or raw graph tool."""
    with revision_lock(root) as root:
        return _hydrate_locked(root)


@contextmanager
def hydrated_revision(root):
    """Hold the revision lock from hydration through child process quiescence."""
    with revision_lock(root) as root:
        _hydrate_locked(root)
        yield root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('hydrate',))
    parser.add_argument('revision_root', type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(hydrate_revision(args.revision_root)))


if __name__ == '__main__':
    main()
