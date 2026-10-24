"""Linux transfer behavior, staging protection and concurrent offset regression."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sys
import pytest

if sys.platform != "linux":
    pytest.skip("Linux file transfer implementation", allow_module_level=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import file_transfer as transfer

@pytest.fixture
def staging(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    monkeypatch.setattr(transfer, "ROOT", root)
    return tmp_path, root

def begin(tmp_path, payload, digest=None):
    target = tmp_path / "target.bin"
    item = transfer.begin_upload(str(target), len(payload), digest or hashlib.sha256(payload).hexdigest())
    return target, item["transfer_id"]

def test_multichunk_exact_hash_and_private_staging_cleanup(staging):
    tmp_path, root = staging
    payload = bytes(range(256)) * 160 + b"final"
    target, identity = begin(tmp_path, payload)
    folder = root / identity
    assert root.stat().st_mode & 0o777 == 0o700
    assert folder.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir())
    for offset in range(0, len(payload), transfer.CHUNK):
        part = payload[offset:offset + transfer.CHUNK]
        result = transfer.upload_chunk(identity, offset, base64.b64encode(part).decode())
        assert result["offset"] == offset + len(part)
    result = transfer.finish_upload(identity)
    assert result["bytes"] == len(payload) and target.read_bytes() == payload
    assert not folder.exists()
    assert not list(tmp_path.glob(".aster-upload-*"))
    chunks = [base64.b64decode(transfer.download_chunk(str(target), offset)["data_base64"])
              for offset in range(0, len(payload), transfer.CHUNK)]
    assert b"".join(chunks) == payload
    assert transfer.download_info(str(target))["sha256"] == hashlib.sha256(payload).hexdigest()

def test_bad_offset_incomplete_and_hash_rejection_preserve_destination(staging):
    tmp_path, root = staging
    target, identity = begin(tmp_path, b"abc", digest="0" * 64)
    with pytest.raises(ValueError, match="offset"):
        transfer.upload_chunk(identity, 1, base64.b64encode(b"a").decode())
    with pytest.raises(ValueError, match="incomplete"):
        transfer.finish_upload(identity)
    transfer.upload_chunk(identity, 0, base64.b64encode(b"abc").decode())
    with pytest.raises(ValueError, match="SHA256"):
        transfer.finish_upload(identity)
    assert not target.exists()
    assert (root / identity / "data").read_bytes() == b"abc"

def test_publication_refuses_sentinel_created_after_begin(staging):
    tmp_path, root = staging
    target, identity = begin(tmp_path, b"abc")
    transfer.upload_chunk(identity, 0, base64.b64encode(b"abc").decode())
    target.write_bytes(b"sentinel")
    with pytest.raises(ValueError, match="destination exists"):
        transfer.finish_upload(identity)
    assert target.read_bytes() == b"sentinel"
    assert not list(tmp_path.glob(".aster-upload-*"))

def test_concurrent_same_offset_has_one_winner(staging):
    tmp_path, root = staging
    target, identity = begin(tmp_path, b"abcabc")
    def append():
        try:
            return transfer.upload_chunk(identity, 0, base64.b64encode(b"abc").decode())
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: append(), range(2)))
    assert sum(item is not None for item in outcomes) == 1
    assert (root / identity / "data").read_bytes() == b"abc"

def test_unprotected_staging_and_symlink_download_rejected(staging):
    tmp_path, root = staging
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(PermissionError):
        begin(tmp_path, b"abc")
    target = tmp_path / "real"
    target.write_bytes(b"abc")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        transfer.download_info(str(link))

def test_invalid_staging_root_rejected_before_any_creation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(transfer, "ROOT", Path("unexpected-relative-staging"))
    with pytest.raises(ValueError, match="absolute canonical"):
        transfer.begin_upload(str(tmp_path / "target"), 0, hashlib.sha256(b"").hexdigest())
    assert not (tmp_path / "unexpected-relative-staging").exists()
