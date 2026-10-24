"""Linux transfers with protected staging, serialized offsets and create-only publication."""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat

ROOT = Path(os.environ.get("ASTER_TRANSFER_ROOT", "/var/lib/aster-universal-mcp/transfers"))
CHUNK = 16384

def _protected(path, *, directory=False):
    value = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o077:
        raise PermissionError("transfer state must be privately owned")
    return value

def _path(transfer_id):
    if not isinstance(transfer_id, str) or len(transfer_id) != 32 or any(c not in "0123456789abcdef" for c in transfer_id):
        raise ValueError("invalid transfer ID")
    if not ROOT.is_absolute():
        raise ValueError("absolute transfer root required")
    _protected(ROOT, directory=True)
    folder = ROOT / transfer_id
    _protected(folder, directory=True)
    return folder

@contextmanager
def _locked(transfer_id):
    folder = _path(transfer_id)
    descriptor = os.open(folder / "lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o077:
            raise PermissionError("unprotected transfer lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _protected(folder, directory=True)
        yield folder
    finally:
        os.close(descriptor)

def _metadata(folder):
    _protected(folder / "meta")
    return json.loads((folder / "meta").read_text(encoding="utf-8"))

def begin_upload(destination: str, size: int, sha256: str):
    if not ROOT.is_absolute() or ROOT.resolve() != ROOT:
        raise ValueError("absolute canonical transfer root required")
    if not isinstance(destination, str) or not Path(destination).is_absolute():
        raise ValueError("absolute destination required")
    if type(size) is not int or size < 0 or size > 2_147_483_648:
        raise ValueError("invalid file size")
    if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("invalid SHA256")
    dest = Path(destination)
    if not dest.parent.is_dir() or dest.is_symlink() or dest.exists():
        raise ValueError("destination must be a new file in an existing directory")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    _protected(ROOT, directory=True)
    transfer_id = secrets.token_hex(16)
    folder = ROOT / transfer_id
    folder.mkdir(mode=0o700)
    try:
        for name in ("meta", "data", "lock"):
            fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                if name == "meta":
                    out.write((json.dumps({"destination": destination, "size": size, "sha256": sha256}) + "\n").encode())
                out.flush()
                os.fsync(out.fileno())
    except Exception:
        shutil.rmtree(folder)
        raise
    return {"transfer_id": transfer_id, "offset": 0, "chunk_bytes": CHUNK}

def upload_chunk(transfer_id: str, offset: int, data_base64: str):
    if type(offset) is not int or offset < 0:
        raise ValueError("invalid offset")
    if not isinstance(data_base64, str) or len(data_base64) > 4 * ((CHUNK + 2) // 3):
        raise ValueError("chunk too large")
    data = base64.b64decode(data_base64, validate=True)
    if len(data) > CHUNK:
        raise ValueError("chunk too large")
    with _locked(transfer_id) as folder:
        expected = _metadata(folder)["size"]
        _protected(folder / "data")
        fd = os.open(folder / "data", os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(fd, "r+b") as output:
            current = output.seek(0, 2)
            if offset != current or current + len(data) > expected:
                raise ValueError("unexpected offset")
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    return {"transfer_id": transfer_id, "offset": current + len(data)}

def finish_upload(transfer_id: str):
    with _locked(transfer_id) as folder:
        meta = _metadata(folder)
        data = folder / "data"
        _protected(data)
        if data.stat().st_size != meta["size"]:
            raise ValueError("incomplete upload")
        hasher = hashlib.sha256()
        with open(data, "rb") as source:
            for part in iter(lambda: source.read(1048576), b""):
                hasher.update(part)
        if hasher.hexdigest() != meta["sha256"]:
            raise ValueError("SHA256 mismatch")
        dest = Path(meta["destination"])
        if dest.exists() or dest.is_symlink():
            raise ValueError("destination exists")
        temporary = dest.parent / (".aster-upload-" + secrets.token_hex(16))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as out, open(data, "rb") as source:
                shutil.copyfileobj(source, out, length=1048576)
                out.flush()
                os.fsync(out.fileno())
            os.link(temporary, dest, follow_symlinks=False)
            parent_fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            temporary.unlink(missing_ok=True)
        for name in ("meta", "data", "lock"):
            (folder / name).unlink()
        folder.rmdir()
    return {"status": "completed", "path": str(dest), "bytes": meta["size"], "sha256": meta["sha256"]}

@contextmanager
def _read_regular(path):
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("absolute regular file required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("regular file required")
        with os.fdopen(fd, "rb", closefd=False) as source:
            yield source
    finally:
        os.close(fd)

def download_info(path: str):
    digest = hashlib.sha256()
    with _read_regular(path) as source:
        for part in iter(lambda: source.read(1048576), b""):
            digest.update(part)
        size = os.fstat(source.fileno()).st_size
    return {"bytes": size, "sha256": digest.hexdigest(), "chunk_bytes": CHUNK}

def download_chunk(path: str, offset: int, limit: int = CHUNK):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= CHUNK:
        raise ValueError("invalid range")
    with _read_regular(path) as source:
        source.seek(offset)
        data = source.read(limit)
        size = os.fstat(source.fileno()).st_size
    return {"offset": offset, "bytes": len(data), "data_base64": base64.b64encode(data).decode("ascii"),
            "eof": offset + len(data) >= size}
