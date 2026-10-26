"""Guard the destructive boundary and tar extraction of disposable recovery tests."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


restore = load("archive_restore_qualifier", "scripts/archive-restore-qualify.py")
probe = load("archive_recovery_probe", "deploy/archive/recovery_probe.py")


def state():
    token = "1" * 32
    database = "aster_archive_restore_" + token[:24]
    return {"token": token, "database": database,
            "volumes": {kind: database + "_" + kind for kind in ("objects", "auth", "config")}}


@pytest.mark.parametrize("changed", [
    {"database": "aster_archive"},
    {"token": "../"},
    {"volumes": {"objects": "aster-archive_archive_objects", "auth": "x", "config": "y"}},
])
def test_cleanup_rejects_unscoped_target_before_any_command(tmp_path, monkeypatch, changed):
    value = {**state(), **changed}
    monkeypatch.setattr(restore, "command", lambda *a, **k: pytest.fail("must not reach Docker"))
    with pytest.raises(ValueError):
        restore.cleanup(value, tmp_path / "state.json", io.BytesIO())


def test_cleanup_rejects_reused_database_marker_before_drop(tmp_path, monkeypatch):
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return b"aster-archive-restore:a-different-run\n"
    monkeypatch.setattr(restore, "command", fake)
    with pytest.raises(ValueError, match="marker mismatch"):
        restore.cleanup(state(), tmp_path / "state.json", io.BytesIO())
    assert len(calls) == 1
    assert "dropdb" not in calls[0]


def test_commands_without_input_get_devnull(monkeypatch):
    observed = {}
    def fake(argv, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(stdout=b"safe")
    monkeypatch.setattr(restore.subprocess, "run", fake)
    assert restore.command(["unused"], log=io.BytesIO(), capture=True) == b"safe"
    assert observed["stdin"] == subprocess.DEVNULL


def test_restore_list_removes_only_privileged_extension_entries():
    ordinary = "4; 1259 17001 TABLE public synthetic aster_archive_ingest\n5; 0 0 COMMENT public TABLE synthetic aster_archive_ingest\n"
    source = ("; Archive TOC\n2; 3079 17000 EXTENSION - vector \n"
              "3; 0 0 COMMENT - EXTENSION vector \n" + ordinary)
    filtered, extensions = restore.prepare_restore_list(source)
    assert extensions == ["vector"]
    assert filtered == "; Archive TOC\n" + ordinary
    assert b"vector WITH SCHEMA public" in restore.EXTENSION_DDL["vector"]


def test_restore_list_refuses_unreviewed_admin_extension():
    with pytest.raises(ValueError, match="unsupported privileged extension"):
        restore.prepare_restore_list("2; 3079 17000 EXTENSION - unreviewed_extension \n")


def make_backup(path):
    entries = {}
    for name in ("database.dump", "files.tar.gz", "deployment.env"):
        data = ("synthetic-" + name).encode()
        (path / name).write_bytes(data)
        entries[name] = hashlib.sha256(data).hexdigest()
    (path / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in entries.items()))


def test_checked_backup_detects_changed_content(tmp_path):
    make_backup(tmp_path)
    assert restore.checked_backup(tmp_path) == tmp_path
    (tmp_path / "database.dump").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore.checked_backup(tmp_path)


def test_checked_backup_rejects_manifest_path_escape(tmp_path):
    make_backup(tmp_path)
    (tmp_path / "SHA256SUMS").write_text("0" * 64 + "  ../deployment.env\n")
    with pytest.raises(ValueError, match="unexpected checksum"):
        restore.checked_backup(tmp_path)


def tar_bytes(name, data=b"test", typeflag=tarfile.REGTYPE):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        item = tarfile.TarInfo(name)
        item.type = typeflag
        item.size = len(data) if typeflag == tarfile.REGTYPE else 0
        item.linkname = "/outside"
        archive.addfile(item, io.BytesIO(data) if item.size else None)
    return stream.getvalue()


def extractor_setup(tmp_path, monkeypatch, data):
    for name in ("archive", "auth", "config"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(probe.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
    monkeypatch.setattr(probe.shutil, "disk_usage", lambda p: SimpleNamespace(free=1024**4))
    monkeypatch.setattr(probe.os, "chown", lambda *a, **k: None)


@pytest.mark.parametrize("name,typeflag", [
    ("archive/../../outside", tarfile.REGTYPE),
    ("/archive/outside", tarfile.REGTYPE),
    ("archive/link", tarfile.SYMTYPE),
    ("archive/hardlink", tarfile.LNKTYPE),
    ("config/unexpected-secret", tarfile.REGTYPE),
])
def test_extract_rejects_escape_links_and_unexpected_private_state(tmp_path, monkeypatch, name, typeflag):
    extractor_setup(tmp_path, monkeypatch, tar_bytes(name, typeflag=typeflag))
    with pytest.raises(ValueError):
        probe.extract_stream(1000, tmp_path)
    assert not (tmp_path.parent / "outside").exists()


def test_extract_enforces_budget_before_file_write(tmp_path, monkeypatch):
    extractor_setup(tmp_path, monkeypatch, tar_bytes("archive/item", b"12345"))
    with pytest.raises(ValueError, match="byte budget"):
        probe.extract_stream(4, tmp_path)
    assert not (tmp_path / "archive/item").exists()


def test_extract_valid_small_backup_preserves_exact_bytes(tmp_path, monkeypatch):
    stream = io.BytesIO()
    members = {"archive/objects/example": b"synthetic\x00original", "auth/oauth.sqlite3": b"sqlite-placeholder",
               "config/clients.json": json.dumps({"version": 1, "clients": {}}).encode()}
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in members.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), 0o600
            archive.addfile(item, io.BytesIO(data))
    extractor_setup(tmp_path, monkeypatch, stream.getvalue())
    result = probe.extract_stream(1024, tmp_path)
    assert result["files"] == 3
    for name, data in members.items():
        assert (tmp_path / name).read_bytes() == data
