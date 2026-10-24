"""Opt-in gates using a functioning native root systemd manager.

Set ASTER_NATIVE_SYSTEMD_TESTS=1 only inside the disposable acceptance VM.
Every service and file is test-owned and UUID-isolated.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

if sys.platform != "linux":
    pytest.skip("native Linux execution", allow_module_level=True)
import admin_jobs as jobs

pytestmark = pytest.mark.skipif(os.environ.get("ASTER_NATIVE_SYSTEMD_TESTS") != "1" or os.geteuid() != 0,
                                reason="requires explicitly selected disposable root systemd VM")


@pytest.fixture
def native(tmp_path, monkeypatch):
    assert Path("/proc/1/comm").read_text().strip() == "systemd"
    assert Path("/sys/fs/cgroup/cgroup.controllers").exists()
    root = tmp_path / "jobs"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(jobs, "JOB_ROOT", root)
    yield tmp_path
    pending = []
    for folder in root.iterdir():
        if folder.is_dir():
            result = jobs.job_status(folder.name)
            if result["status"] in jobs.PENDING:
                result = jobs.cancel_job(folder.name)
            if result["status"] in jobs.PENDING:
                pending.append(folder.name)
    assert pending == [], "acceptance-owned lifecycle operation remains pending"


def settle(job_id, seconds=12):
    deadline = time.monotonic() + seconds
    while True:
        state = jobs.job_status(job_id)
        if state["status"] not in jobs.PENDING:
            assert state["unit_quiescent"] and not state["lifecycle_pending"]
            return state
        assert time.monotonic() < deadline, state
        time.sleep(0.05)


def tree_script(root, parent_exits=False, ignore_term=False):
    identities = root / "identities.json"
    effects = root / "child-effects.txt"
    child = "import os,time,signal; " + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "") + "f=open(%r,'a',buffering=1);\nwhile True: f.write('tick\\n'); time.sleep(0.04)" % str(effects)
    code = "\n".join([
        "import os,subprocess,sys,time,json",
        "from pathlib import Path",
        "def identity(pid):",
        " s=Path('/proc/'+str(pid)+'/stat').read_text(); return {'pid':pid,'start_ticks':int(s[s.rfind(')')+2:].split()[19]),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}",
        "child=subprocess.Popen([sys.executable,'-c',%r],start_new_session=True)" % child,
        "Path(%r).write_text(json.dumps([identity(os.getpid()),identity(child.pid)]))" % str(identities),
        "print('parent-and-setsid-child-started',flush=True)",
        "time.sleep(0.25)" if parent_exits else "time.sleep(60)",
    ])
    return code, identities, effects


def wait_file(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline
        time.sleep(0.03)
    return json.loads(path.read_text())


def assert_tree_quiet(identities, effects):
    for identity in identities:
        assert not jobs._same_process(identity), identity
    size = effects.stat().st_size if effects.exists() else 0
    time.sleep(0.2)
    assert (effects.stat().st_size if effects.exists() else 0) == size


def test_native_success_failure_and_bounded_streams(native):
    done = jobs.run_command([sys.executable, "-c", "import os; print('hello'); os.write(2,b'error-stream'); raise SystemExit(7)"], str(native), 3)
    assert done["status"] == "completed" and done["exit_code"] == 7
    assert done["stdout"] == "hello\n" and done["stderr"] == "error-stream"
    failed = jobs.run_command(["/nonexistent/aster-native-command"], str(native), 3)
    assert failed["status"] == "failed" and failed["exit_code"] is None
    bounded = jobs.run_command([sys.executable, "-c", "import os; os.write(1,b'x'*2097152); os.write(2,b'y'*2097152)"], str(native), 5)
    assert bounded["status"] == "completed" and bounded["exit_code"] == 0
    for stream in ("stdout", "stderr"):
        assert len(bounded[stream]) == jobs.MAX_OUTPUT
        assert bounded[stream + "_truncated"] and bounded[stream + "_capture_truncated"]
        assert (jobs.JOB_ROOT / bounded["job_id"] / (stream + ".txt")).stat().st_size == jobs.MAX_SPOOL


def test_native_timeout_kills_parent_and_setsid_child(native):
    code, identities_file, effects = tree_script(native)
    started = time.monotonic()
    result = jobs.run_command([sys.executable, "-c", code], str(native), 1)
    elapsed = time.monotonic() - started
    assert result["status"] == "timeout" and result["unit_quiescent"]
    assert elapsed < 8
    assert_tree_quiet(json.loads(identities_file.read_text()), effects)


def test_native_completion_cleans_remaining_setsid_child(native):
    code, identities_file, effects = tree_script(native, parent_exits=True)
    result = jobs.run_command([sys.executable, "-c", code], str(native), 5)
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert_tree_quiet(json.loads(identities_file.read_text()), effects)


def test_native_cancel_stops_worker_and_children(native):
    code, identities_file, effects = tree_script(native)
    start = jobs.start_job([sys.executable, "-c", code], str(native), 30)
    identities = wait_file(identities_file)
    running = jobs.job_status(start["job_id"])
    assert running["status"] == "running" and running["invocation_id"]
    worker = running["worker_identity"]
    cancelled = jobs.cancel_job(start["job_id"])
    assert cancelled["status"] == "cancelled" and cancelled["cancelled"] and cancelled["unit_quiescent"]
    assert_tree_quiet([worker, *identities], effects)
    again = jobs.cancel_job(start["job_id"])
    assert not again["cancelled"] and again["status"] == "cancelled"


def test_native_interrupted_worker_reconciles_after_actual_death(native):
    code, identities_file, effects = tree_script(native)
    start = jobs.start_job([sys.executable, "-c", code], str(native), 30)
    identities = wait_file(identities_file)
    running = jobs.job_status(start["job_id"])
    snap = jobs._unit_snapshot(running["unit"])
    assert jobs._unit_identity_ok(running, snap)
    killed = subprocess.run(["systemctl", "kill", "--kill-whom=main", "--signal=SIGKILL", running["unit"]], capture_output=True, timeout=5)
    assert killed.returncode == 0
    state = settle(start["job_id"])
    assert state["status"] == "interrupted"
    assert_tree_quiet([running["worker_identity"], *identities], effects)


def test_native_cancel_completion_race_preserves_settled_state(native):
    # Exercise real competing worker/status/cancel writes without unbounded load.
    for _ in range(3):
        start = jobs.start_job([sys.executable, "-c", "import time; time.sleep(.08); print('race-finished')"], str(native), 3)
        cancelled = jobs.cancel_job(start["job_id"])
        settled = settle(start["job_id"])
        assert settled["status"] in ("completed", "cancelled")
        assert cancelled["status"] in ("completed", "cancelled")
        time.sleep(0.1)
        assert jobs.job_status(start["job_id"])["status"] == settled["status"]
        assert not list((jobs.JOB_ROOT / start["job_id"]).glob(".*.tmp"))


def test_native_cancel_escalates_for_setsid_child_ignoring_sigterm(native):
    code, identities_file, effects = tree_script(native, ignore_term=True)
    start = jobs.start_job([sys.executable, "-c", code], str(native), 30)
    identities = wait_file(identities_file)
    ready_deadline = time.monotonic() + 3
    # First tick is written only after the child installs SIG_IGN. Avoid a
    # timing-dependent test which cancels before the signal policy exists.
    while not effects.exists() or effects.stat().st_size == 0:
        assert time.monotonic() < ready_deadline
        time.sleep(0.02)
    running = jobs.job_status(start["job_id"])
    began = time.monotonic()
    cancelled = jobs.cancel_job(start["job_id"])
    elapsed = time.monotonic() - began
    assert cancelled["cancelled"] and cancelled["unit_quiescent"]
    assert 1.5 <= elapsed < 8, elapsed
    assert_tree_quiet([running["worker_identity"], *identities], effects)
