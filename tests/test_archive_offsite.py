"""Credentials, retries, and retention safety for the encrypted archive wrapper."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setattr(value, "local_retention", lambda: pytest.fail("must not prune before retrying pending upload"))
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


def test_local_retention_default_and_explicit_count_are_validated(drive_configuration):
    path, cfg = drive_configuration
    assert offsite.config(path)["local_backups_to_keep"] == 3
    path.write_text(json.dumps(cfg | {"local_backups_to_keep": 1}))
    assert offsite.config(path)["local_backups_to_keep"] == 1
    path.write_text(json.dumps(cfg | {"local_backups_to_keep": 7}))
    assert offsite.config(path)["local_backups_to_keep"] == 7


@pytest.mark.parametrize("count", [0, 8, -1, True, False, 1.0, "1", None])
def test_invalid_local_retention_count_is_rejected(drive_configuration, count):
    path, cfg = drive_configuration
    path.write_text(json.dumps(cfg | {"local_backups_to_keep": count}))
    with pytest.raises(ValueError, match="local_backups_to_keep"):
        offsite.config(path)


@pytest.fixture
def managed_backups(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("managed local retention requires root-owned private backups")
    value = runner(tmp_path, monkeypatch)
    parent = Path(value.cfg["backup_dir"])
    parent.mkdir(mode=0o700)
    receipts = value.state / "snapshots"
    receipts.mkdir(mode=0o700)
    paths = []
    rows = []
    for index in range(5):
        path = parent / f"aster-archive-202610{index + 1:02d}T000000Z-fixture{index}"
        path.mkdir(mode=0o700)
        lines = []
        for name in ("database.dump", "files.tar.gz", "deployment.env"):
            member = path / name
            member.write_bytes(f"synthetic backup {index} member {name}\n".encode())
            member.chmod(0o600)
            lines.append(f"{offsite.digest(member)}  {name}\n")
        (path / "SHA256SUMS").write_text("".join(lines))
        (path / "SHA256SUMS").chmod(0o600)
        row = {"snapshot_id": f"{index + 1:064x}", "backup": str(path),
               "uploaded_at": f"2026-10-{index + 1:02d}T00:00:00Z",
               "structural_check": True,
               "manifest_sha256": offsite.digest(path / "SHA256SUMS")}
        offsite.recovery.save(receipts / (row["snapshot_id"] + ".json"), row)
        paths.append(path)
        rows.append(row)
    monkeypatch.setattr(value, "anchor", lambda: rows[0])
    monkeypatch.setattr(value, "snapshots", lambda: [{"id": row["snapshot_id"]} for row in rows])
    calls = []
    monkeypatch.setattr(value, "identity", lambda: calls.append("identity"))
    def check(*args, **kwargs):
        assert args == ("check",)
        assert all(path.is_dir() for path in paths)
        calls.append("check")
        return b""
    monkeypatch.setattr(value, "restic", check)
    return value, paths, rows, calls


@pytest.mark.parametrize("count,removed,kept", [(None, 1, (0, 2, 3, 4)), (1, 3, (0, 4))])
def test_managed_local_retention_preserves_anchor_and_configured_recent_backups(managed_backups, count, removed, kept):
    value, paths, rows, calls = managed_backups
    if count is not None:
        value.cfg["local_backups_to_keep"] = count
    assert value.local_retention() == removed
    assert [index for index, path in enumerate(paths) if path.exists()] == list(kept)
    assert calls == ["identity", "check"]


def test_local_retention_preserves_pending_unuploaded_and_augmented_backups(managed_backups):
    value, paths, rows, calls = managed_backups
    value.cfg["local_backups_to_keep"] = 1
    offsite.recovery.save(value.state / "pending-local-backup.json", {"backup": str(paths[1])})
    # An unuploaded local copy has no eligible receipt even if it is old.
    (value.state / "snapshots" / (rows[2]["snapshot_id"] + ".json")).unlink()
    (paths[3] / "operator-note.txt").write_text("preserve augmented backup")
    assert value.local_retention() == 0
    assert all(path.is_dir() for path in paths)
    assert calls == []


def test_failed_remote_check_preserves_every_local_copy(managed_backups, monkeypatch):
    value, paths, rows, calls = managed_backups
    value.cfg["local_backups_to_keep"] = 1
    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic remote check failure")
    monkeypatch.setattr(value, "restic", unavailable)
    with pytest.raises(RuntimeError, match="remote check"):
        value.local_retention()
    assert all(path.is_dir() for path in paths)


def test_changed_local_backup_bytes_prevent_deletion(managed_backups):
    value, paths, rows, calls = managed_backups
    value.cfg["local_backups_to_keep"] = 1
    (paths[3] / "database.dump").write_bytes(b"changed without a matching manifest")
    with pytest.raises(ValueError, match="checksum mismatch"):
        value.local_retention()
    assert all(path.is_dir() for path in paths)
    assert calls == []


def test_daily_prunes_uploaded_copies_before_capacity_measurement_and_after_upload(tmp_path, monkeypatch):
    value = runner(tmp_path, monkeypatch)
    monkeypatch.setattr(value, "anchor", lambda: {})
    events = []
    pending = value.state / "pending-local-backup.json"
    calls = []
    def retain():
        calls.append(1)
        events.append("retain")
        assert pending.exists() == (len(calls) == 2)
        return len(calls)
    monkeypatch.setattr(value, "local_retention", retain)
    monkeypatch.setattr(value, "stage", lambda *args, **kwargs: None)
    monkeypatch.setattr(offsite.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 1024**3))
    backup = str(tmp_path / "backups" / "aster-archive-20261026T000000Z-new")
    def run(argv, **kwargs):
        if argv[0] == "bash":
            events.append("create")
            return ("Completed local backup: " + backup + "\n").encode()
        events.append("measure")
        assert events[0] == "retain"
        return b"1\n"
    monkeypatch.setattr(value, "run", run)
    def upload(path):
        assert path == backup and pending.exists()
        events.append("upload")
        return {"snapshot_id": "f" * 64}
    monkeypatch.setattr(value, "upload", upload)
    result = value.daily()
    assert events == ["retain", "measure", "measure", "create", "upload", "retain"]
    assert result["local_managed_backups_removed"] == 3
    assert not pending.exists()
