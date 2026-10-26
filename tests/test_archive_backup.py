"""Cold process backup regression using synthetic SQLite, CAS, and client ACL."""

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_backup_script_cold_python_preserves_cas_acl_and_committed_wal(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir(mode=0o700)
    original = b"synthetic original archive bytes\x00\xff\n"
    digest = hashlib.sha256(original).hexdigest()
    object_path = archive / "objects" / "sha256" / digest[:2] / digest
    object_path.parent.mkdir(parents=True, mode=0o700)
    object_path.write_bytes(original)
    object_path.chmod(0o400)

    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    clients = config / "clients.json"
    acl = json.dumps({"synthetic-client": {"owner_id": "synthetic-owner"}}).encode()
    clients.write_bytes(acl)
    clients.chmod(0o600)

    auth = tmp_path / "auth"
    auth.mkdir(mode=0o700)
    database = auth / "oauth.sqlite3"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE synthetic_state(value TEXT NOT NULL)")
        connection.execute("INSERT INTO synthetic_state VALUES (?)", ("committed synthetic WAL row",))
        connection.commit()
        database.chmod(0o600)
        # Keep the source connection open: a correct SQLite snapshot includes
        # committed WAL data, not merely the current bytes of the main DB file.
        assert Path(str(database) + "-wal").stat().st_size > 0

        # Execute the actual deployment script with no site preloading. Do not
        # use -I/-P: its directory must be on sys.path to catch stdlib shadows.
        result = subprocess.run([
            sys.executable, "-E", "-S", "-B",
            str(ROOT / "deploy/archive/backup_state.py"),
        ], cwd=ROOT, env={
            **os.environ,
            "ASTER_ARCHIVE_ROOT": str(archive),
            "ASTER_ARCHIVE_OAUTH_DB": str(database),
            "ASTER_ARCHIVE_CLIENTS": str(clients),
        }, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    finally:
        connection.close()

    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as backup:
        object_member = "archive/" + object_path.relative_to(archive).as_posix()
        assert backup.extractfile(object_member).read() == original
        assert backup.getmember(object_member).mode == 0o400
        assert backup.extractfile("config/clients.json").read() == acl
        assert backup.getmember("config/clients.json").mode == 0o600
        assert backup.getmember("auth/oauth.sqlite3").mode == 0o600
        assert not any(name.endswith(("-wal", "-shm")) for name in backup.getnames())
        snapshot = tmp_path / "restored-oauth.sqlite3"
        snapshot.write_bytes(backup.extractfile("auth/oauth.sqlite3").read())
        snapshot.chmod(0o600)

    with sqlite3.connect(snapshot) as restored:
        assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT value FROM synthetic_state").fetchall() == [
            ("committed synthetic WAL row",),
        ]
    assert object_path.read_bytes() == original
    assert clients.read_bytes() == acl


@pytest.mark.parametrize("fail_dump", [False, True])
@pytest.mark.parametrize("writers", [("watch", "embedding", "cognee"),
                                    ("watch", "worker", "embedding", "knowledge", "cognee")])
def test_backup_resumes_only_originally_running_writers_even_on_failure(tmp_path, fail_dump, writers):
    scripts = tmp_path / "repo/scripts"
    deploy = tmp_path / "repo/deploy/archive"
    scripts.mkdir(parents=True)
    deploy.mkdir(parents=True)
    script = scripts / "archive-backup.sh"
    script.write_bytes((ROOT / "scripts/archive-backup.sh").read_bytes())
    (deploy / ".env").write_text("SYNTHETIC_CONFIG=not-a-secret\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    log = tmp_path / "docker.jsonl"
    docker.write_text("#!/usr/bin/env python3\n" + '''
import json, os, sys
args = sys.argv[1:]
action_index = next(i for i, value in enumerate(args) if value in {"ps", "stop", "start", "exec", "run"})
action, tail = args[action_index], args[action_index + 1:]
with open(os.environ["SYNTHETIC_DOCKER_LOG"], "a") as stream:
    stream.write(json.dumps([action, tail]) + "\\n")
if action == "ps":
    print("db\\nmcp\\nunrelated-agent\\n" + os.environ["SYNTHETIC_RUNNING_WRITERS"])
elif action == "exec":
    if os.environ["SYNTHETIC_FAIL_DUMP"] == "1":
        sys.exit(17)
    sys.stdout.buffer.write(b"synthetic PostgreSQL dump")
elif action == "run":
    sys.stdout.buffer.write(b"synthetic files snapshot")
''')
    docker.chmod(0o700)
    destination = tmp_path / "backups"
    completed = subprocess.run(["bash", str(script), str(destination)], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=20, env={**os.environ,
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "SYNTHETIC_DOCKER_LOG": str(log), "SYNTHETIC_FAIL_DUMP": "1" if fail_dump else "0",
        "SYNTHETIC_RUNNING_WRITERS": "\n".join(writers)})
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [tail for action, tail in calls if action == "stop"] == [["--timeout", "120", *writers]]
    assert [tail for action, tail in calls if action == "start"] == [list(writers)]
    assert calls[-1][0] == "start"
    if fail_dump:
        assert completed.returncode != 0
        assert not list(destination.glob("aster-archive-*"))
        assert len(list(destination.glob(".aster-archive-backup.*"))) == 1
        assert not any(action == "run" for action, _ in calls)
    else:
        assert completed.returncode == 0
        backup, = destination.glob("aster-archive-*")
        assert {item.name for item in backup.iterdir()} == {"database.dump", "files.tar.gz", "deployment.env", "SHA256SUMS"}
        subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=backup, stdin=subprocess.DEVNULL,
                       capture_output=True, check=True, timeout=5)
