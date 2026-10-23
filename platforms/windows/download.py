"""Windows beta file download metadata and bounded chunks."""
import base64
import hashlib
from pathlib import Path

CHUNK_BYTES = 16384

def download_info(path: str):
    source = Path(path)
    if not source.is_file():
        raise ValueError("file required")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            digest.update(block)
    return {"bytes": source.stat().st_size, "sha256": digest.hexdigest(), "chunk_bytes": CHUNK_BYTES}

def download_chunk(path: str, offset: int, limit: int = CHUNK_BYTES):
    source = Path(path)
    if not source.is_file() or not isinstance(offset, int) or offset < 0:
        raise ValueError("invalid source or offset")
    if not isinstance(limit, int) or not 1 <= limit <= CHUNK_BYTES:
        raise ValueError("invalid chunk size")
    with source.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(limit)
    return {"offset": offset, "bytes": len(chunk), "data_base64": base64.b64encode(chunk).decode("ascii"),
            "eof": offset + len(chunk) >= source.stat().st_size}
