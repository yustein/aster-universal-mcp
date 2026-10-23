"""Resumable bounded file transfers; files staged privately, SHA256 verified."""
import base64
import hashlib
import os
import secrets
import shutil
from pathlib import Path

ROOT = Path("/var/lib/aster-universal-mcp/transfers")
CHUNK = 16384

def _path(transfer_id):
    if not isinstance(transfer_id, str) or len(transfer_id) != 32 or any(c not in "0123456789abcdef" for c in transfer_id):
        raise ValueError("invalid transfer ID")
    return ROOT / transfer_id

def begin_upload(destination: str, size: int, sha256: str):
    if not isinstance(destination, str) or not Path(destination).is_absolute():
        raise ValueError("absolute destination required")
    if not isinstance(size, int) or size < 0 or size > 2_147_483_648:
        raise ValueError("invalid file size")
    if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("invalid SHA256")
    dest = Path(destination)
    if not dest.parent.is_dir() or dest.is_symlink() or dest.exists():
        raise ValueError("destination must be a new file in an existing directory")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    transfer_id = secrets.token_hex(16)
    folder = _path(transfer_id)
    folder.mkdir(mode=0o700)
    (folder / "meta").write_text(f"{destination}\n{size}\n{sha256}\n")
    fd = os.open(folder / "data", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return {"transfer_id": transfer_id, "offset": 0, "chunk_bytes": CHUNK}

def upload_chunk(transfer_id: str, offset: int, data_base64: str):
    folder = _path(transfer_id)
    expected = int((folder / "meta").read_text().splitlines()[1])
    data = base64.b64decode(data_base64, validate=True)
    if len(data) > CHUNK:
        raise ValueError("chunk too large")
    with open(folder / "data", "r+b") as f:
        current = f.seek(0, 2)
        if offset != current or current + len(data) > expected:
            raise ValueError("unexpected offset")
        f.write(data)
        f.flush()
    return {"transfer_id": transfer_id, "offset": current + len(data)}

def finish_upload(transfer_id: str):
    folder = _path(transfer_id)
    destination, size, digest = (folder / "meta").read_text().splitlines()
    data = folder / "data"
    if data.stat().st_size != int(size):
        raise ValueError("incomplete upload")
    hasher = hashlib.sha256()
    with open(data, "rb") as f:
        for part in iter(lambda: f.read(1048576), b""):
            hasher.update(part)
    if hasher.hexdigest() != digest:
        raise ValueError("SHA256 mismatch")
    dest = Path(destination)
    if dest.exists() or dest.is_symlink():
        raise ValueError("destination exists")
    # Stage on destination filesystem, then atomically create-only publish.
    temporary = dest.parent / (".aster-upload-" + secrets.token_hex(16))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out, open(data, "rb") as source:
            shutil.copyfileobj(source, out, length=1048576)
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, dest, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)
    (folder / "meta").unlink()
    data.unlink()
    folder.rmdir()
    return {"status": "completed", "path": str(dest), "bytes": int(size), "sha256": digest}

def download_info(path: str):
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("regular file required")
    h = hashlib.sha256()
    with open(source, "rb") as f:
        for part in iter(lambda: f.read(1048576), b""):
            h.update(part)
    return {"bytes": source.stat().st_size, "sha256": h.hexdigest(), "chunk_bytes": CHUNK}

def download_chunk(path: str, offset: int, limit: int = CHUNK):
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("regular file required")
    if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= CHUNK:
        raise ValueError("invalid range")
    with open(source, "rb") as f:
        f.seek(offset)
        data = f.read(limit)
    return {"offset": offset, "bytes": len(data), "data_base64": base64.b64encode(data).decode("ascii"),
            "eof": offset + len(data) >= source.stat().st_size}
