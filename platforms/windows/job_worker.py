"""Background worker for locally launched Windows jobs."""
import json
import subprocess
import sys
import time
from jobs import job_path, save_status
from boot_identity import boot_id
from job_object import run_contained
from process_identity import creation_time_100ns

def main(job_id):
    folder = job_path(job_id)
    request_file = folder / "request.json"
    request = json.loads(request_file.read_text(encoding="utf-8"))
    status_file = folder / "status.json"
    # Launcher must publish the worker identity before the worker runs code.
    deadline = time.monotonic() + 10
    record = {}
    while time.monotonic() < deadline:
        try:
            record = json.loads(status_file.read_text(encoding="utf-8"))
        except (PermissionError, FileNotFoundError):
            time.sleep(0.02)
            continue
        if (record.get("worker_pid") == __import__("os").getpid()
                and record.get("worker_creation_time_100ns") == creation_time_100ns(__import__("os").getpid())
                and record.get("boot_id") == boot_id()):
            break
        time.sleep(0.02)
    else:
        from status_transition import transition
        transition(folder, "failed", diagnostic_code="identity_handshake_timeout",
                   pid_match=record.get("worker_pid") == __import__("os").getpid(),
                   observed_worker_pid=__import__("os").getpid(),
                   creation_present=bool(record.get("worker_creation_time_100ns")),
                   boot_present=bool(record.get("boot_id")))
        return
    from status_transition import transition
    _, changed = transition(folder, "running")
    if not changed:
        return
    request_file.unlink(missing_ok=True)
    try:
        with (folder / "stdout.txt").open("wb") as stdout, (folder / "stderr.txt").open("wb") as stderr:
            result = run_contained(request["command"], cwd=request["cwd"],
                                   stdout=stdout, stderr=stderr, timeout=request["max_runtime"])
        save_status(job_id, "completed", result.returncode)
    except subprocess.TimeoutExpired:
        save_status(job_id, "timeout")
    except OSError:
        save_status(job_id, "failed")

if __name__ == "__main__":
    main(sys.argv[1])
