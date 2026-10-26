"""SFTP backup configuration and statvfs guards; no external connections."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_offsite_sftp", ROOT / "scripts/archive-offsite.py")
offsite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offsite)


@pytest.fixture
def sftp_config(tmp_path, monkeypatch):
    if os.geteuid() != 0 or not Path("/usr/bin/ssh-keygen").exists():
        pytest.skip("private operator configuration requires root and system OpenSSH")
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    identity = root / "id_ed25519"
    subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=True)
    identity.chmod(0o600)
    known = root / "known_hosts"
    known.write_text(offsite.SFTP_ALIAS + " " + " ".join(identity.with_suffix(".pub").read_text().split()[:2]) + "\n")
    known.chmod(0o600)
    ssh = root / "ssh_config"
    entries = {"host": offsite.SFTP_ALIAS, "hostname": "synthetic-backup.example.invalid", "user": "synthetic-backup",
               "port": "22", "identityfile": str(identity), "userknownhostsfile": str(known),
               **offsite.SFTP_REQUIRED, "connecttimeout": "15", "connectionattempts": "1",
               "serveraliveinterval": "30", "serveralivecountmax": "3", "loglevel": "ERROR"}
    ssh.write_text("\n".join(key + " " + value for key, value in entries.items()) + "\n")
    ssh.chmod(0o600)
    monkeypatch.setattr(offsite, "SFTP_CONFIG", ssh)
    monkeypatch.setattr(offsite.socket, "gethostname", lambda: "synthetic-lenomila")
    password = root / "password"
    password.write_bytes(b"synthetic-restic-password-for-local-tests-only\n")
    password.chmod(0o600)
    cfg = {"expected_hostname": "synthetic-lenomila", "repository_type": "sftp", "repository": offsite.SFTP_REPOSITORY,
           "ssh_config": str(ssh), "ssh_known_hosts": str(known), "ssh_identity_file": str(identity),
           "password_file": str(password), "restic_program": "/usr/bin/true", "backup_dir": str(root / "backups"),
           "state_dir": str(root / "state"), "host": "synthetic-archive",
           "key_escrow": {"verified": True, "reference": "https://chatgpt.com/library?file_id=synthetic",
                          "password_sha256": hashlib.sha256(password.read_bytes()).hexdigest()}}
    path = root / "offsite.json"
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    return path, cfg


def runner(sftp_config):
    path, _ = sftp_config
    cfg = offsite.config(path)
    work = Path(cfg["state_dir"]) / "run"
    work.mkdir(mode=0o700)
    return offsite.Offsite(cfg, work, io.BytesIO())


def test_sftp_needs_no_drive_config_and_cannot_inherit_agent_or_askpass(sftp_config, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/unrelated/agent")
    monkeypatch.setenv("SSH_ASKPASS", "/untrusted/helper")
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
    monkeypatch.setenv("RCLONE_CONFIG", "/unrelated/drive")
    value = runner(sftp_config)
    assert "drive_root_folder_id" not in value.cfg and "rclone_config" not in value.cfg
    assert not any(key.startswith(("RCLONE_", "SSH_ASKPASS")) for key in value.env)
    assert "SSH_AUTH_SOCK" not in value.env
    assert value.env["PATH"] == "/usr/bin:/bin" and value.env["LC_ALL"] == "C"
    assert "sftp.args=-F " + value.cfg["ssh_config"] in value.base
    assert "sftp.connections=2" in value.base
    # -G reads local configuration only: no socket or remote command is opened.
    effective = subprocess.run(["/usr/bin/ssh", "-G", "-F", value.cfg["ssh_config"], offsite.SFTP_ALIAS],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=5).stdout
    settings = dict(line.split(" ", 1) for line in effective.splitlines())
    assert settings["batchmode"] == "yes" and settings["passwordauthentication"] == "no"
    assert settings["stricthostkeychecking"] in ("yes", "true")
    assert settings["userknownhostsfile"] == value.cfg["ssh_known_hosts"]


@pytest.mark.parametrize("before,after", [
    ("batchmode yes", "batchmode no"),
    ("stricthostkeychecking yes", "stricthostkeychecking accept-new"),
    ("passwordauthentication no", "passwordauthentication yes"),
    ("identityagent none", "identityagent SSH_AUTH_SOCK"),
    ("host aster-archive-backup", "host *"),
    ("permitlocalcommand no", "include /unreviewed/config"),
])
def test_sftp_rejects_prompts_fallback_and_unreviewed_configuration(sftp_config, before, after):
    path, cfg = sftp_config
    ssh = Path(cfg["ssh_config"])
    ssh.write_text(ssh.read_text().replace(before, after))
    with pytest.raises(ValueError):
        offsite.config(path)


@pytest.mark.parametrize("repository", [
    "sftp:another-host:/repository", "sftp:aster-archive-backup:repository",
    "sftp:aster-archive-backup:/srv/aster-archive-offsite/repository",
    "sftp:aster-archive-backup:/repository/../outside",
])
def test_repository_alias_and_chroot_path_are_exact(sftp_config, repository):
    path, cfg = sftp_config
    path.write_text(json.dumps(cfg | {"repository": repository}))
    with pytest.raises(ValueError, match="exact dedicated"):
        offsite.config(path)


def test_pinned_host_key_and_identity_files_must_stay_private(sftp_config):
    path, cfg = sftp_config
    known = Path(cfg["ssh_known_hosts"])
    known.chmod(0o640)
    with pytest.raises(ValueError, match="private"):
        offsite.config(path)
    known.chmod(0o600)
    known.write_text(known.read_text().replace("aster-archive-backup", "another-host"))
    with pytest.raises(ValueError, match="pin exactly"):
        offsite.config(path)


def test_repository_pin_binds_sftp_configuration_without_overwriting(sftp_config, monkeypatch):
    value = runner(sftp_config)
    monkeypatch.setattr(value, "restic", lambda *a, **k: json.dumps({"id": "a" * 64, "version": 2}).encode())
    identity = value.identity(register=True)
    assert identity["repository_type"] == "sftp" and "drive_root_folder_id" not in identity
    pin = value.state / "repository.json"
    original = pin.read_bytes()
    ssh = Path(value.cfg["ssh_config"])
    ssh.write_text(ssh.read_text() + "# a changed operator record\n")
    with pytest.raises(ValueError, match="identity changed"):
        value.identity(register=True)
    assert pin.read_bytes() == original


def capacity_report(available):
    available //= 1024
    return ("Size Used Avail (root) %Capacity\n"
            f"{available + 4096} 1024 {available} {available + 1024} 0%\nsftp> quit\n").encode()


def test_parser_uses_unprivileged_available_capacity():
    assert offsite.sftp_available_bytes(capacity_report(60 * 1024**3)) == 60 * 1024**3
    with pytest.raises(ValueError):
        offsite.sftp_available_bytes(b"statvfs is not supported\n")
    with pytest.raises(ValueError):
        offsite.sftp_available_bytes(b"Size Used Avail (root) %Capacity\n100 1 200 100 1%\n")


@pytest.mark.parametrize("available,passes", [(50 * 1024**3, False), (50 * 1024**3 + 1024, True)])
def test_capacity_guard_uses_sftp_only_and_requires_reserve_plus_backup(sftp_config, monkeypatch, available, passes):
    value = runner(sftp_config)
    def report(argv, **kwargs):
        assert argv == ["/usr/bin/sftp", "-F", value.cfg["ssh_config"], "-b", "-", offsite.SFTP_ALIAS]
        assert kwargs["input"] == b"@df /repository\nquit\n" and kwargs["timeout"] == 60
        return capacity_report(available)
    monkeypatch.setattr(value, "run", report)
    if passes:
        assert value.remote_capacity(1)["available_bytes"] == available
    else:
        with pytest.raises(ValueError, match="50 GiB"):
            value.remote_capacity(1)
    assert json.loads((value.work / "remote-capacity.json").read_text())["required_bytes"] == 50 * 1024**3 + 1


def test_low_remote_capacity_blocks_restic_upload(sftp_config, monkeypatch):
    value = runner(sftp_config)
    backup = Path(value.cfg["backup_dir"]) / "synthetic-completed-backup"
    backup.mkdir(mode=0o700)
    manifest = []
    for name in ("database.dump", "files.tar.gz", "deployment.env"):
        data = ("synthetic " + name).encode()
        (backup / name).write_bytes(data)
        manifest.append(hashlib.sha256(data).hexdigest() + "  " + name + "\n")
    (backup / "SHA256SUMS").write_text("".join(manifest))
    monkeypatch.setattr(value, "run", lambda *a, **k: capacity_report(50 * 1024**3))
    monkeypatch.setattr(value, "restic", lambda *a, **k: pytest.fail("must not upload below remote reserve"))
    with pytest.raises(ValueError, match="50 GiB"):
        value.upload(backup)


def test_capacity_parser_accepts_actual_local_openssh_statvfs():
    server = Path("/usr/lib/openssh/sftp-server")
    if not server.exists() or not Path("/usr/bin/sftp").exists():
        pytest.skip("requires local OpenSSH SFTP client/server for stdio-only capacity check")
    # -D starts a local protocol server over stdio, never an SSH/network session.
    output = subprocess.run(["/usr/bin/sftp", "-D", str(server), "-b", "-", "localhost"],
                            input=b"@df /tmp\nquit\n", capture_output=True, timeout=10, check=True,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout
    assert offsite.sftp_available_bytes(output) > 0
