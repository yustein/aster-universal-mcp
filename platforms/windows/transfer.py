"""Windows-native resumable file transfer primitives for local beta."""
import base64
import hashlib
import os
import secrets
from pathlib import Path

ROOT = Path(os.environ.get("ASTER_WINDOWS_DATA", str(Path.home() / "AppData" / "Local" / "AsterUniversalMCP"))) / "transfers"
CHUNK = 16384

def begin_upload(destination: str, size: int, sha256: str):
    target = Path(destination)
    if not target.is_absolute() or target.exists() or not target.parent.is_dir():
        raise ValueError("new absolute destination required")
    if not isinstance(size, int) or not 0 <= size <= 2147483648:
        raise ValueError("invalid size")
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("invalid sha256")
    ROOT.mkdir(parents=True, exist_ok=True)
    transfer_id = secrets.token_hex(16)
    folder = ROOT / transfer_id
    folder.mkdir()
    (folder / "meta").write_text(f"{target}\n{size}\n{sha256}", encoding="utf-8")
    (folder / "data").touch()
    return {"transfer_id": transfer_id, "offset": 0, "chunk_bytes": CHUNK}

def upload_chunk(transfer_id: str, offset: int, data_base64: str):
    if len(transfer_id) != 32 or any(c not in "0123456789abcdef" for c in transfer_id):
        raise ValueError("invalid transfer ID")
    folder = ROOT / transfer_id
    data = base64.b64decode(data_base64, validate=True)
    if len(data) > CHUNK:
        raise ValueError("chunk too large")
    size = int((folder / "meta").read_text(encoding="utf-8").splitlines()[1])
    current = (folder / "data").stat().st_size
    if current != offset or current + len(data) > size:
        raise ValueError("unexpected offset")
    with (folder / "data").open("ab") as f:
        f.write(data)
    return {"transfer_id": transfer_id, "offset": current + len(data)}
