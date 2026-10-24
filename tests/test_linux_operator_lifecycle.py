"""Native operator controls, independent of application imports and OAuth fixtures.

Use a root-protected pytest --basetemp path on the Linux qualification VM.
These tests do not substitute for native MCP/public HTTPS acceptance.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

if sys.platform != "linux":
    pytest.skip("Linux /proc and pidfds required", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("linux_operator_under_test", ROOT / "tools/linux_operator.py")
operator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc and pidfds required")


def test_pidfd_refuses_reused_or_mismatched_creation_identity():
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"])
    try:
        original = operator.process_identity(child.pid)
        wrong = dict(original, start_ticks=str(int(original["start_ticks"]) + 1))
        with pytest.raises(PermissionError, match="different identity"):
            operator.stop_one(wrong, grace=0.1)
        assert child.poll() is None
        assert operator.stop_one(original, grace=1) == "stopped"
        child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=2)


def test_pidfd_escalates_only_the_identified_term_ignoring_process(tmp_path):
    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c",
                              "import signal,time,pathlib,sys;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                              "pathlib.Path(sys.argv[1]).write_text('ready');time.sleep(20)", str(ready)])
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        identity = operator.process_identity(child.pid)
        assert operator.stop_one(identity, grace=0.1) == "stopped"
        assert child.wait(timeout=2) == -9
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_protected_path_rejects_symlink_and_writable_controls(tmp_path):
    if os.geteuid() or os.getegid():
        pytest.skip("root-owned controls are required for this native permission fixture")
    control = tmp_path / "control"
    control.write_text("data")
    control.chmod(0o666)
    with pytest.raises(PermissionError, match="writable"):
        operator.protected_path(control, ancestors=False)
    control.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(control)
    with pytest.raises(PermissionError, match="canonical"):
        operator.protected_path(link, ancestors=False)


@pytest.fixture
def deployment(tmp_path):
    if os.geteuid() or os.getegid():
        pytest.skip("native protected operator tests require root")
    try:
        operator.protected_path(tmp_path, directory=True)
    except PermissionError:
        pytest.skip("run with --basetemp under a root-protected directory, outside /tmp and user home")
    if subprocess.run(["systemctl", "show", "--property=Version", "--value"],
                      capture_output=True, timeout=5).returncode:
        pytest.skip("native lifecycle fixture requires a running systemd manager")
    source, venv, state = [tmp_path / name for name in ("source", "venv", "state")]
    source.mkdir(mode=0o700)
    venv.mkdir(mode=0o700)
    (venv / "bin").mkdir(mode=0o700)
    (venv / "bin/python").symlink_to(Path(sys.executable).resolve())
    (venv / "pyvenv.cfg").write_text("# operator identity fixture; not a product runtime\n")
    (source / "admin_executor_local.py").write_text(
        "import os,socket,time\n"
        "s=socket.socket(socket.AF_UNIX);s.bind(os.environ['ASTER_ADMIN_SOCKET']);"
        "os.chmod(os.environ['ASTER_ADMIN_SOCKET'],0o600);s.listen();time.sleep(60)\n")
    (source / "oauth_service.py").write_text(
        "import os,socket,time\n"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        "s.bind(('127.0.0.1',int(os.environ['ASTER_MCP_PORT'])));s.listen();time.sleep(60)\n")
    # Keep the source marker inventory exact, independently of installed recovery code.
    manifest = {"schema_version": 2, "source_commit": "1" * 40, "files": [
        {"path": path.name, "sha256": operator.sha256(path), "bytes": path.stat().st_size,
         "mode": path.stat().st_mode & 0o777} for path in sorted(source.iterdir())]}
    (source / "candidate-manifest.json").write_text(json.dumps(manifest))
    state.mkdir(mode=0o700)
    for name in ("run", "jobs", "transfers", "workspace", "logs"):
        (state / name).mkdir(mode=0o700)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = {"schema_version": 1, "state": str(state), "source": str(source), "venv": str(venv),
              "python": str(venv / "bin/python"), "origin": "https://operator.example.invalid", "port": port,
              "manifest_sha256": operator.sha256(source / "candidate-manifest.json"),
              "paths": {"oauth_db": str(state / "oauth.sqlite"), "allowlist": str(state / "admin-client-allowlist.json"),
                        "workspace": str(state / "workspace"), "socket": str(state / "run/admin.sock"),
                        "jobs": str(state / "jobs"), "transfers": str(state / "transfers"),
                        "audit": str(state / "logs/executor.jsonl")}}
    operator.atomic_json(state / "config.json", config)
    operator.atomic_json(state / "admin-client-allowlist.json", ["client-a", "client-b"])
    with sqlite3.connect(state / "oauth.sqlite") as db:
        db.execute("CREATE TABLE oauth_clients(client_id,name,redirect_uris,revoked)")
        db.execute("CREATE TABLE client_metadata(client_id)")
        db.execute("INSERT INTO oauth_clients VALUES ('client-c','Fixture','[\"https://example.invalid/callback\"]',0)")
        db.execute("INSERT INTO client_metadata VALUES ('client-c')")
    (state / "oauth.sqlite").chmod(0o600)
    operator.atomic_json(state / "venv-manifest.json", operator.venv_inventory(venv))
    shutil.copyfile(ROOT / "tools/linux_operator.py", state / "operator-linux.py")
    (state / "operator-linux.py").chmod(0o600)
    install = {"config_sha256": operator.sha256(state / "config.json"),
               "operator_sha256": operator.sha256(state / "operator-linux.py"),
               "venv_manifest_sha256": operator.sha256(state / "venv-manifest.json")}
    operator.atomic_json(state / "installation.json", install)

    def run(*args):
        return subprocess.run(["/usr/bin/python3", str(state / "operator-linux.py"), "--state", str(state), *args],
                              capture_output=True, text=True, timeout=50)

    fixture = {"source": source, "venv": venv, "state": state, "config": config,
               "installation": install, "run": run}
    yield fixture
    # Test-owned processes only, with original creation identities preserved.
    if (state / "runtime.json").exists():
        for identity in operator.read_runtime(config).get("processes", {}).values():
            if operator.process_alive(identity):
                operator.stop_one(identity, grace=1)


def success(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_recovery_survives_missing_source_and_venv(deployment):
    f = deployment
    started = success(f["run"]("start"))
    assert started["ready"]
    f["source"].rename(f["source"].with_name("source-unavailable"))
    f["venv"].rename(f["venv"].with_name("venv-unavailable"))
    assert f["run"]("approve", "client-c").returncode != 0
    status = success(f["run"]("status"))
    assert status["ready"] and not status["source_verified"] and not status["venv_verified"]
    revoked = success(f["run"]("revoke", "client-a"))
    assert revoked["oauth_credentials_revoked"] is False
    assert operator.allowlist(f["config"]) == ["client-b"]
    disabled = success(f["run"]("disable"))
    assert disabled["status"] == "disabled"
    assert (f["state"] / "access-disabled.json").exists()
    assert operator.allowlist(f["config"]) == []
    assert all(not operator.process_alive(item) for item in operator.read_runtime(f["config"])["processes"].values())


def test_source_drift_blocks_new_grants_but_not_stop(deployment):
    f = deployment
    success(f["run"]("start"))
    (f["source"] / "ordinary-output.txt").write_text("unexpected output inside frozen source")
    assert f["run"]("approve", "client-c").returncode != 0
    assert f["run"]("start").returncode != 0
    assert operator.allowlist(f["config"]) == ["client-a", "client-b"]
    success(f["run"]("stop"))


def test_missing_process_record_is_reconciled_without_duplicate_start(deployment):
    f = deployment
    success(f["run"]("start"))
    runtime = operator.read_runtime(f["config"])
    gateway_pid = runtime["processes"].pop("gateway")["pid"]
    runtime["operation"] = {"action": "start", "status": "needs_reconciliation"}
    operator.atomic_json(f["state"] / "runtime.json", runtime)
    status = success(f["run"]("status"))
    assert status["ready"]
    assert status["processes"]["gateway"]["identity"]["pid"] == gateway_pid
    restarted = success(f["run"]("start"))
    assert restarted["status"] == "already_running"
    assert restarted["processes"]["gateway"]["identity"]["pid"] == gateway_pid


def test_config_redirection_fails_before_revoking_anything(deployment):
    f = deployment
    outsider = f["state"].parent / "unrelated.json"
    operator.atomic_json(outsider, ["preserve"])
    config = dict(f["config"])
    config["paths"] = dict(config["paths"], allowlist=str(outsider))
    operator.atomic_json(f["state"] / "config.json", config)
    result = f["run"]("disable")
    assert result.returncode != 0 and "configuration changed" in result.stderr
    assert json.loads(outsider.read_text()) == ["preserve"]
    assert json.loads((f["state"] / "admin-client-allowlist.json").read_text()) == ["client-a", "client-b"]


def test_disabled_flag_requires_explicit_enable_and_does_not_restore_grants(deployment):
    f = deployment
    success(f["run"]("start"))
    success(f["run"]("disable"))
    assert f["run"]("start").returncode != 0
    enabled = success(f["run"]("enable"))
    assert enabled["grants_restored"] is False and enabled["processes_started"] is False
    assert operator.allowlist(f["config"]) == []
    success(f["run"]("start"))
    success(f["run"]("approve", "client-c"))
    assert operator.allowlist(f["config"]) == ["client-c"]


def test_concurrent_operator_mutation_is_rejected(deployment):
    f = deployment
    with operator.operator_lock(f["state"]):
        result = f["run"]("revoke", "client-a")
    assert result.returncode != 0
    assert operator.allowlist(f["config"]) == ["client-a", "client-b"]
