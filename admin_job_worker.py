"""Transient-systemd worker with bounded, streaming output capture.

The command shares the worker's cgroup.  On command completion or timeout the
worker records a provisional outcome and exits.  systemd then removes remaining
children (including ordinary setsid descendants); the controller only publishes
a settled outcome after the unit and cgroup are quiescent.  Approved root code
can deliberately escape or reconfigure the manager; this is not a sandbox.
"""
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path
from admin_jobs import _job_dir, _locked, _read_state, _atomic, process_identity, MAX_SPOOL


def capture(command, cwd, limit, folder):
    """Retain at most 1 MiB/stream, discard excess without accumulating memory."""
    outputs = {}
    counts = {"stdout": 0, "stderr": 0}
    proc = None
    selector = selectors.DefaultSelector()
    outcome = {"outcome_status": "failed", "exit_code": None}
    try:
        for name in counts:
            fd = os.open(folder / (name + ".txt"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            outputs[name] = os.fdopen(fd, "wb", buffering=0)
        proc = subprocess.Popen(command, cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        deadline = time.monotonic() + limit
        drain_deadline = None
        while True:
            now = time.monotonic()
            code = proc.poll()
            if code is None and now >= deadline:
                outcome.update(outcome_status="timeout", exit_code=None)
                break
            if code is not None:
                if drain_deadline is None:
                    drain_deadline = now + 0.15
                if not selector.get_map() or now >= drain_deadline:
                    outcome.update(outcome_status="completed", exit_code=code)
                    break
            for key, _ in selector.select(0.03):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                remaining = max(0, MAX_SPOOL - counts[name])
                if remaining:
                    outputs[name].write(chunk[:remaining])
                counts[name] += len(chunk)
    except (OSError, ValueError, subprocess.SubprocessError):
        outcome.update(outcome_status="failed", exit_code=None, error="execution failed")
    finally:
        selector.close()
        if proc is not None:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        for handle in outputs.values():
            os.fsync(handle.fileno())
            handle.close()
    outcome.update({name + "_bytes_observed": count for name, count in counts.items()})
    return outcome


def main(job_id):
    folder = _job_dir(job_id)
    with _locked(folder):
        state = _read_state(folder)
        invocation = os.environ.get("INVOCATION_ID", "")
        cgroup = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[-1]
        if not invocation or not cgroup.endswith("/" + state["unit"]):
            raise RuntimeError("worker must run in its dedicated systemd unit")
        if state.get("invocation_id") not in (None, invocation):
            raise RuntimeError("worker invocation mismatch")
        if state.get("cancel_requested") or state["status"] not in ("queued", "launch_uncertain"):
            return
        fd = os.open(folder / "request.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        (folder / "request.json").unlink()
        state.update(status="running", worker_identity=process_identity(os.getpid()),
                     worker_pid=os.getpid(), started_at=time.time(), invocation_id=invocation,
                     cgroup=cgroup, launch_in_progress=False)
        _atomic(folder / "status.json", state)
    outcome = capture(payload["command"], payload["cwd"], payload["max_runtime"], folder)
    with _locked(folder):
        state = _read_state(folder)
        # A cancellation or terminal reconciliation wins over a late worker write.
        if state["status"] in ("running", "finishing") and not state.get("cancel_requested"):
            state.update(outcome)
            state.update(status="finishing", worker_finished_at=time.time(), lifecycle_pending=True)
            _atomic(folder / "status.json", state)


if __name__ == "__main__":
    main(sys.argv[1])
