"""Windows beta upload completion with verified create-only publication."""
import hashlib
import os
import secrets
import shutil
from pathlib import Path
from transfer import ROOT

def finish_upload(transfer_id: str):
    if len(transfer_id) != 32 or any(ch not in "0123456789abcdef" for ch in transfer_id):
        raise ValueError("invalid transfer id")
    folder = ROOT / transfer_id
    destination, expected_size, expected_hash = (folder / "meta").read_text(encoding="utf-8").splitlines()
    source = folder / "data"
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            digest.update(block)
    if source.stat().st_size != int(expected_size) or digest.hexdigest() != expected_hash:
        raise ValueError("size or checksum mismatch")
    target = Path(destination)
    if target.exists() or not target.parent.is_dir():
        raise ValueError("destination unavailable")
    temporary = target.parent / (".aster-upload-" + secrets.token_hex(16))
    try:
        with temporary.open("xb") as output, source.open("rb") as handle:
            shutil.copyfileobj(handle, output, length=1048576)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    (folder / "meta").unlink()
    source.unlink()
    folder.rmdir()
    return {"status": "completed", "bytes": int(expected_size), "sha256": expected_hash}
