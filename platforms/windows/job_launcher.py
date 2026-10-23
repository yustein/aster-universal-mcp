"""Launch detached local Windows background jobs."""
import json
import subprocess
import sys
import uuid
from pathlib import Path
from jobs import ROOT, save_status
from boot_identity import boot_id
from process_identity import creation_time_100ns

def start_job(command: list[str], cwd: str = ".", max_runtime: int = 3600):
    if not isinstance(command, list) or not command or any(not isinstance(part, str) for part in command):
        raise ValueError("invalid command")
    if not isinstance(max_runtime, int) or not 1 <= max_runtime <= 86400:
        raise ValueError("invalid runtime")
    directory = Path(cwd).resolve()
    if not directory.is_dir():
        raise ValueError("invalid directory")
    job_id = str(uuid.uuid4())
    save_status(job_id, "queued")
    folder = ROOT / job_id
    (folder / "request.json").write_text(
        json.dumps({"command": command, "cwd": str(directory), "max_runtime": max_runtime}), encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    # Never require a developer checkout .venv: a clean beta installation
    # may use a per-user or system Python without a sibling virtualenv.
    # A venv redirector can spawn a second interpreter with a different PID.
    # Use the base interpreter so Popen PID remains the actual worker PID.
    base_executable = Path(getattr(sys, "_base_executable", sys.executable))
    if not base_executable.is_file():
        raise RuntimeError("Windows base Python executable unavailable")
    python_executable = base_executable
    with (folder / "worker-launch.log").open("ab") as diagnostic:
        worker = subprocess.Popen(
            [str(python_executable), str(Path(__file__).with_name("job_worker.py")), job_id],
            stdin=subprocess.DEVNULL, stdout=diagnostic, stderr=subprocess.STDOUT,
            creationflags=flags, close_fds=True)
    from jobs import job_path
    status_file = job_path(job_id) / "status.json"
    from status_transition import transition
    try:
        created = creation_time_100ns(worker.pid)
        if not created:
            raise RuntimeError("Windows worker process identity unavailable")
        record, changed = transition(folder, "queued", worker_pid=worker.pid,
                                     worker_creation_time_100ns=created, boot_id=boot_id())
        if not changed:
            raise RuntimeError("Windows worker identity publication rejected")
    except Exception:
        # Never leave an unverified worker with an apparently runnable job.
        # A termination error makes the outcome uncertain, not successful.
        termination_confirmed = False
        try:
            worker.terminate()
            worker.wait(timeout=10)
            termination_confirmed = True
        except Exception:
            termination_confirmed = worker.poll() is not None
        transition(folder, "failed" if termination_confirmed else "worker_termination_unverified",
                   diagnostic_code="worker_identity_publication_failed")
        raise
    return {"job_id": job_id, "status": "queued", "worker_pid": worker.pid}
