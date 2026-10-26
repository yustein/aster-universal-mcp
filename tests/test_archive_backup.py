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
