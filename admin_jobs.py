"""Durable asynchronous job execution for local administrator socket.

Job metadata contains no command arguments or output. The worker receives
the command on stdin, never via its own process arguments.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

JOB_ROOT = Path("/var/lib/aster-universal-mcp/admin-jobs")
MAX_OUTPUT = 16384

def _job_dir(job_id):
    if not isinstance(job_id, str) or len(job_id) != 36:
        raise ValueError("invalid job id")
    if str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid job id")
    return JOB_ROOT / job_id

def _atomic(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def start_job(command, cwd, max_runtime=3600):
    if not isinstance(max_runtime, int) or not 1 <= max_runtime <= 86400:
        raise ValueError("max_runtime must be 1..86400")
    JOB_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())
    folder = _job_dir(job_id)
    folder.mkdir(mode=0o700)
    _atomic(folder / "status.json", {"job_id": job_id, "status": "queued",
                                     "created_at": time.time(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "exit_code": None})
    # Short-lived root-only input file; never place sensitive argv in unit metadata.
    request_file = folder / "request.json"
    fd = os.open(request_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"command": command, "cwd": cwd, "max_runtime": max_runtime}, handle)
    unit = "aster-mcp-job-" + job_id
    launch = subprocess.run(["systemd-run", "--collect", "--unit=" + unit,
        "--property=RuntimeMaxSec=" + str(max_runtime + 30),
        sys.executable, str(Path(__file__).with_name("admin_job_worker.py")), job_id],
        capture_output=True, timeout=10, check=False)
    if launch.returncode != 0:
        request_file.unlink(missing_ok=True)
        _atomic(folder / "status.json", {"job_id": job_id, "status": "launch_failed", "exit_code": None})
        return {"status": "launch_failed", "job_id": job_id}
    return {"status": "queued", "job_id": job_id, "unit": unit}

def job_status(job_id):
    folder = _job_dir(job_id)
    data = json.loads((folder / "status.json").read_text())
    if data["status"] in ("queued", "running"):
        if data.get("boot_id") != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            data["status"] = "interrupted_by_reboot"
            (folder / "request.json").unlink(missing_ok=True)
            _atomic(folder / "status.json", data)
            return data
        pid = data.get("worker_pid")
        if pid and not Path(f"/proc/{pid}").exists():
            data["status"] = "interrupted"
            (folder / "request.json").unlink(missing_ok=True)
            _atomic(folder / "status.json", data)
    for stream in ("stdout", "stderr"):
        path = folder / (stream + ".txt")
        if path.exists():
            with open(path, "rb") as f:
                data[stream] = f.read(MAX_OUTPUT).decode("utf-8", "replace")
                data[stream + "_truncated"] = path.stat().st_size > MAX_OUTPUT
    return data

def reconcile_after_boot():
    if not JOB_ROOT.exists():
        return 0
    count = 0
    for folder in JOB_ROOT.iterdir():
        if not folder.is_dir():
            continue
        status_file = folder / "status.json"
        try:
            data = json.loads(status_file.read_text())
            if data.get("status") in ("queued", "running"):
                data["status"] = "interrupted_by_restart"
                _atomic(status_file, data)
                count += 1
        except (OSError, ValueError):
            continue
    return count


def cancel_job(job_id):
    folder = _job_dir(job_id)
    state = json.loads((folder / "status.json").read_text())
    if state.get("status") not in ("queued", "running"):
        return {"job_id": job_id, "status": state.get("status"), "cancelled": False}
    unit = "aster-mcp-job-" + job_id + ".service"
    result = subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=15, check=False)
    if result.returncode != 0:
        return {"job_id": job_id, "status": "cancel_failed", "cancelled": False}
    state = json.loads((folder / "status.json").read_text())
    if state.get("status") in ("queued", "running"):
        state["status"] = "cancelled"
        state["finished_at"] = time.time()
        _atomic(folder / "status.json", state)
    (folder / "request.json").unlink(missing_ok=True)
    return {"job_id": job_id, "status": state["status"], "cancelled": state["status"] == "cancelled"}
