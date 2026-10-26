"""Credentials, retries, and retention safety for the encrypted archive wrapper."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_offsite", ROOT / "scripts/archive-offsite.py")
offsite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offsite)


def runner(tmp_path, monkeypatch, recovery_password=None):
    password = tmp_path / "password"
    password.write_bytes(b"synthetic-test-password-never-a-real-key\n")
    password.chmod(0o600)
    work = tmp_path / "run"
    work.mkdir()
    cfg = {"password_file": str(password), "repository": "rclone:ty gdrive:synthetic-test-repository",
           "drive_root_folder_id": "synthetic-private-root-folder",
           "rclone_config": str(tmp_path / "rclone.conf"), "restic_program": "/synthetic/restic",
           "rclone_program": "/synthetic/rclone", "state_dir": str(tmp_path),
           "backup_dir": str(tmp_path / "backups"), "host": "synthetic-host",
           "key_escrow": {"password_sha256": hashlib.sha256(password.read_bytes()).hexdigest()}}
    monkeypatch.setattr(offsite, "private_file", lambda path, **kwargs: Path(path))
    return offsite.Offsite(cfg, work, io.BytesIO(), recovery_password)


def test_private_file_rejects_symlink_and_group_access(tmp_path):
    path = tmp_path / "private"
    path.write_text("synthetic")
    path.chmod(0o600)
    assert offsite.private_file(path, root=False) == path
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        offsite.private_file(link, root=False)
    path.chmod(0o640)
    with pytest.raises(ValueError):
        offsite.private_file(path, root=False)


def test_runner_forces_password_file_and_exact_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIC_PASSWORD", "must-not-propagate")
    monkeypatch.setenv("RESTIC_PASSWORD_COMMAND", "untrusted-command")
    monkeypatch.setenv("RESTIC_REPOSITORY_FILE", "/wrong/repository")
    value = runner(tmp_path, monkeypatch)
    assert not {"RESTIC_PASSWORD", "RESTIC_PASSWORD_COMMAND", "RESTIC_REPOSITORY_FILE"} & value.env.keys()
    assert value.env["RESTIC_PASSWORD_FILE"] == str(tmp_path / "password")
    assert value.env["RCLONE_CONFIG"] == str(tmp_path / "rclone.conf")
    assert value.env["RCLONE_DRIVE_ROOT_FOLDER_ID"] == "synthetic-private-root-folder"
    assert "rclone.program=/synthetic/rclone" in value.base
    assert "must-not-propagate" not in " ".join(value.base)


def test_escrow_proof_rejects_operational_file_itself(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="separately retrieved"):
        runner(tmp_path, monkeypatch, tmp_path / "password")


def test_escrow_proof_requires_matching_separate_copy(tmp_path, monkeypatch):
    copy = tmp_path / "downloaded-key"
    copy.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="does not match"):
        runner(tmp_path, monkeypatch, copy)


def test_retry_reuses_pending_backup_without_new_dump(tmp_path, monkeypatch):
    value = runner(tmp_path, monkeypatch)
    pending = tmp_path / "pending-local-backup.json"
    offsite.recovery.save(pending, {"backup": "/existing-completed-backup"})
    monkeypatch.setattr(value, "anchor", lambda: {})
    monkeypatch.setattr(value, "run", lambda *a, **kw: pytest.fail("must not create another local backup"))
    seen = []
    def fail_upload(path):
        seen.append(path)
        raise RuntimeError("synthetic transient failure")
    monkeypatch.setattr(value, "upload", fail_upload)
    with pytest.raises(RuntimeError):
        value.daily()
    assert seen == ["/existing-completed-backup"]
    assert pending.exists()


def test_retry_after_upload_resumes_housekeeping_without_upload(tmp_path, monkeypatch):
    value = runner(tmp_path, monkeypatch)
    snapshot = "e" * 64
    (tmp_path / "snapshots").mkdir()
    receipt = {"snapshot_id": snapshot, "backup": "/existing-completed-backup", "structural_check": True}
    offsite.recovery.save(tmp_path / "snapshots" / (snapshot + ".json"), receipt)
    pending = tmp_path / "pending-local-backup.json"
    offsite.recovery.save(pending, {"backup": receipt["backup"], "snapshot_id": snapshot})
    monkeypatch.setattr(value, "anchor", lambda: {})
    monkeypatch.setattr(value, "run", lambda *a, **k: pytest.fail("must not create a new local backup"))
    monkeypatch.setattr(value, "upload", lambda *a, **k: pytest.fail("must not repeat completed upload"))
    monkeypatch.setattr(value, "local_retention", lambda: 0)
    assert value.daily()["snapshot_id"] == snapshot
    assert not pending.exists()


def test_retention_never_deletes_anchor_or_two_newest(tmp_path, monkeypatch):
    value = runner(tmp_path, monkeypatch)
    ids = [letter * 64 for letter in "abcd"]
    snapshots = [{"id": identity, "time": f"2026-10-{index + 1:02d}T00:00:00Z"} for index, identity in enumerate(ids)]
    monkeypatch.setattr(value, "anchor", lambda: {"snapshot_id": ids[0]})
    monkeypatch.setattr(value, "snapshots", lambda: list(snapshots))
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        if args[0] == "forget" and "--dry-run" in args:
            return json.dumps([{"keep": [snapshots[-1]], "remove": snapshots[:-1]}]).encode()
        if args[0] == "forget":
            assert args == ("forget", ids[1])
            snapshots[:] = [item for item in snapshots if item["id"] != ids[1]]
        return b""
    monkeypatch.setattr(value, "restic", fake)
    result = value.retention(apply=True, prune=True)
    assert result["planned_removals"] == 1 and result["protected_recovery_points"] == 3
    assert ("prune",) in calls
    assert {item["id"] for item in snapshots} == {ids[0], ids[2], ids[3]}


def test_retention_rejects_foreign_snapshot_id_before_mutation(tmp_path, monkeypatch):
    value = runner(tmp_path, monkeypatch)
    anchor = "a" * 64
    monkeypatch.setattr(value, "anchor", lambda: {"snapshot_id": anchor})
    monkeypatch.setattr(value, "snapshots", lambda: [{"id": anchor, "time": "2026-01-01T00:00:00Z"}])
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        return json.dumps([{"keep": [], "remove": [{"id": "f" * 64}]}]).encode()
    monkeypatch.setattr(value, "restic", fake)
    with pytest.raises(ValueError, match="escapes"):
        value.retention(apply=True, prune=True)
    assert len(calls) == 1 and "--dry-run" in calls[0]


def test_prune_can_retry_after_forget_already_succeeded(tmp_path, monkeypatch):
    value = runner(tmp_path, monkeypatch)
    snapshots = [{"id": "a" * 64, "time": "2026-01-01"}, {"id": "b" * 64, "time": "2026-01-02"}]
    monkeypatch.setattr(value, "anchor", lambda: {"snapshot_id": snapshots[0]["id"]})
    monkeypatch.setattr(value, "snapshots", lambda: snapshots)
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        if "--dry-run" in args:
            return json.dumps([{"keep": snapshots, "remove": []}]).encode()
        return b""
    monkeypatch.setattr(value, "restic", fake)
    result = value.retention(apply=True, prune=True)
    assert result["pruned"] is True and ("prune",) in calls
    assert not any(args[0] == "forget" and "--dry-run" not in args for args in calls)


def test_repository_identity_cannot_silently_change(tmp_path, monkeypatch):
    value = runner(tmp_path, monkeypatch)
    first = {"id": "1" * 64, "version": 2}
    second = {"id": "2" * 64, "version": 2}
    monkeypatch.setattr(value, "restic", lambda *a, **k: json.dumps(first).encode())
    assert value.identity(register=True)["id"] == first["id"]
    monkeypatch.setattr(value, "restic", lambda *a, **k: json.dumps(second).encode())
    with pytest.raises(ValueError, match="identity changed"):
        value.identity(register=True)


def test_legacy_drive_repository_pin_remains_valid_without_rewrite(tmp_path, monkeypatch):
    value = runner(tmp_path, monkeypatch)
    monkeypatch.setattr(value, "restic", lambda *a, **k: json.dumps({"id": "1" * 64, "version": 2}).encode())
    identity = value.identity(register=True)
    assert identity.pop("repository_type") == "rclone"
    path = tmp_path / "repository.json"
    offsite.recovery.save(path, identity)
    original = path.read_bytes()
    assert value.identity()["repository_type"] == "rclone"
    assert path.read_bytes() == original


@pytest.fixture
def drive_configuration(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("operator configuration requires root-owned files and directories")
    password = tmp_path / "password"
    password.write_bytes(b"synthetic-owned-client-backup-password-only\n")
    password.chmod(0o600)
    remote = tmp_path / "rclone.conf"
    remote.write_text("[aster-gdrive]\ntype = drive\nclient_id = synthetic-owned-client\n"
                      "client_secret = synthetic-only\ntoken = {\"refresh_token\":\"synthetic-only\"}\n"
                      "[ty gdrive]\ntype = drive\n[other-backend]\ntype = sftp\n")
    remote.chmod(0o600)
    monkeypatch.setattr(offsite.socket, "gethostname", lambda: "synthetic-host")
    cfg = {"expected_hostname": "synthetic-host", "repository_type": "rclone", "rclone_remote": "aster-gdrive",
           "repository": "rclone:aster-gdrive:aster-archive-restic", "drive_root_folder_id": "synthetic-folder-id",
           "password_file": str(password), "rclone_config": str(remote), "restic_program": "/usr/bin/true",
           "rclone_program": "/usr/bin/true", "backup_dir": str(tmp_path / "backups"),
           "state_dir": str(tmp_path / "state"), "host": "synthetic-archive",
           "key_escrow": {"verified": True, "reference": "https://chatgpt.com/library?file_id=synthetic",
                          "password_sha256": hashlib.sha256(password.read_bytes()).hexdigest()}}
    path = tmp_path / "offsite.json"
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    return path, cfg


def test_owned_drive_remote_is_explicit_and_legacy_config_is_unchanged(drive_configuration):
    path, cfg = drive_configuration
    private = Path(cfg["rclone_config"])
    original = private.read_bytes()
    validated = offsite.config(path)
    assert validated["rclone_remote"] == "aster-gdrive"
    assert validated["repository"] == "rclone:aster-gdrive:aster-archive-restic"
    assert private.read_bytes() == original


def test_omitted_remote_preserves_the_legacy_drive_default(drive_configuration):
    path, cfg = drive_configuration
    cfg.pop("rclone_remote")
    cfg["repository"] = "rclone:ty gdrive:aster-archive-restic"
    path.write_text(json.dumps(cfg))
    assert offsite.drive_remote(offsite.config(path)) == "ty gdrive"


@pytest.mark.parametrize("remote,repository", [
    ("aster-gdrive", "rclone:ty gdrive:aster-archive-restic"),
    ("aster-gdrive", "rclone:aster-gdrive-other:aster-archive-restic"),
    ("missing-drive", "rclone:missing-drive:aster-archive-restic"),
    ("other-backend", "rclone:other-backend:aster-archive-restic"),
])
def test_drive_config_rejects_mismatched_missing_or_non_drive_remote(drive_configuration, remote, repository):
    path, cfg = drive_configuration
    path.write_text(json.dumps(cfg | {"rclone_remote": remote, "repository": repository}))
    with pytest.raises(ValueError):
        offsite.config(path)
