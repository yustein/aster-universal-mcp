"""Real process death must not leave an unbounded orphan import running."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux archive worker parent-death guard")
def test_worker_sigkill_terminates_import_child(tmp_path):
    ready = tmp_path / "child-ready"
    child_code = (
        "from aster_archive.queue import _arm_worker_parent; "
        "import os,time,pathlib; _arm_worker_parent(); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import os,subprocess,sys,time; "
        "env=dict(os.environ, ASTER_ARCHIVE_PARENT_PID=str(os.getpid())); "
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}],env=env); time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        child_pid = int(ready.read_text())
        parent.kill()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = Path(f"/proc/{child_pid}/stat")
            if not record.exists() or record.read_text().split(") ", 1)[1].startswith("Z"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("import child survived worker SIGKILL")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform != "linux", reason="Linux archive worker parent-death guard")
def test_child_refuses_parent_that_exited_before_guard():
    result = subprocess.run(
        [sys.executable, "-m", "aster_archive.queue", "_import", "a" * 64, "/tmp"],
        env=dict(os.environ, ASTER_ARCHIVE_PARENT_PID="0"), capture_output=True, timeout=5,
    )
    assert result.returncode == 21
    assert result.stdout == result.stderr == b""
