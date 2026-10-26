"""Local Unix ownership checks for the isolated owner-operation wrapper.

These tests use synthetic SQLite state and never issue or approve credentials.
The root tests exercise real UID ownership and the three-capability boundary;
they do not substitute a claim that Docker/Compose has been qualified.
"""
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

import pytest

from deploy.archive import operator


ROOT = Path(__file__).resolve().parents[1]
ROOT_ONLY = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0,
                               reason="requires an authorized local root Unix ownership test")


@pytest.fixture
def auth_state():
    with tempfile.TemporaryDirectory(prefix="aster-auth-operator-test-", dir="/tmp") as temporary:
        parent = Path(temporary)
        parent.chmod(0o755)
        directory = parent / "auth"
        directory.mkdir(mode=0o700)
        database = directory / operator.DATABASE
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE synthetic_state(value text)")
        connection.execute("INSERT INTO synthetic_state VALUES ('fixture-only')")
        connection.commit()
        database.chmod(0o600)
        os.chown(database, operator.SERVICE_UID, operator.SERVICE_GID)
        os.chown(directory, operator.SERVICE_UID, operator.SERVICE_GID)
        # Keep the connection open so actual WAL/SHM files remain. Root-owned
        # sidecars model the problematic state created by a root-only helper.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(database) + suffix)
            assert sidecar.exists()
            sidecar.chmod(0o600)
            os.chown(sidecar, 0, 0)
        try:
            yield directory, connection
        finally:
            connection.close()


def assert_service_ownership(directory):
    info = directory.stat()
    assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (10001, 10001, 0o700)
    for path in directory.iterdir():
        info = path.stat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (10001, 10001, 0o600)


@ROOT_ONLY
@pytest.mark.skipif(shutil.which("setpriv") is None, reason="requires setpriv for a real capability/UID boundary")
def test_existing_root_helper_works_with_only_three_caps_and_service_can_reopen_wal(auth_state):
    directory, _ = auth_state
    code = """
import pathlib, sys
from deploy.archive.operator import run_operation
effective = int(next(line.split()[1] for line in pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:')), 16)
assert effective == 0b1011, effective
run_operation('revoke', auth_directory=pathlib.Path(sys.argv[1]))
"""
    result = subprocess.run([
        "setpriv", "--bounding-set=-all,+chown,+dac_override,+fowner",
        "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs",
        sys.executable, "-c", code, str(directory),
    ], cwd=ROOT, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "Outstanding pairing code revoked." in result.stdout
    assert_service_ownership(directory)
    service = subprocess.run([
        "setpriv", "--reuid=10001", "--regid=10001", "--clear-groups",
        sys.executable, "-c",
        "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); assert db.execute('SELECT value FROM synthetic_state').fetchone()[0]=='fixture-only'; db.execute(\"INSERT INTO synthetic_state VALUES ('service-write')\"); db.commit(); db.close()",
        str(directory / operator.DATABASE),
    ], cwd="/tmp", text=True, capture_output=True, timeout=20)
    assert service.returncode == 0, service.stderr


@ROOT_ONLY
def test_helper_error_still_restores_database_wal_and_directory_ownership(auth_state):
    directory, _ = auth_state
    with pytest.raises(SystemExit) as result:
        operator.run_operation("inspect", auth_directory=directory, request_id="synthetic-missing-request")
    assert result.value.code == 2
    assert_service_ownership(directory)


@ROOT_ONLY
def test_symlink_database_is_rejected_without_following_or_chowning_target(tmp_path):
    directory = tmp_path / "auth"
    directory.mkdir(mode=0o700)
    target = tmp_path / "outside"
    target.write_text("synthetic untouched data")
    target.chmod(0o640)
    before = target.stat()
    (directory / operator.DATABASE).symlink_to(target)
    with pytest.raises(OSError):
        operator.run_operation("revoke", auth_directory=directory)
    after = target.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    assert target.read_text() == "synthetic untouched data"


def test_wrapper_does_not_bypass_existing_root_requirement(monkeypatch, tmp_path):
    monkeypatch.setattr(operator.os, "geteuid", lambda: 10001)
    with pytest.raises(operator.OperatorError, match="root"):
        operator.run_operation("revoke", auth_directory=tmp_path)


@ROOT_ONLY
def test_unknown_commands_are_rejected_without_invoking_a_helper(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(operator, "_invoke_helper", lambda *args, **kwargs: called.append(args))
    with pytest.raises(operator.OperatorError, match="Unsupported"):
        operator.run_operation("arbitrary-python", auth_directory=tmp_path)
    assert called == []


def test_operator_shell_syntax():
    result = subprocess.run(["sh", "-n", str(ROOT / "deploy/archive/operator.sh")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
