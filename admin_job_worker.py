"""Background worker. Reads sensitive argv from stdin; no argv audit."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from admin_jobs import _job_dir, _atomic

def main(job_id):
    folder = _job_dir(job_id)
    request_file = folder / "request.json"
    payload = json.loads(request_file.read_text(encoding="utf-8"))
    request_file.unlink()
    command = payload["command"]
    cwd = payload["cwd"]
    limit = payload["max_runtime"]
    status = {"job_id": job_id, "status": "running", "worker_pid": os.getpid(),
              "started_at": time.time(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "exit_code": None}
    _atomic(folder / "status.json", status)
    try:
        with open(folder / "stdout.txt", "wb") as out, open(folder / "stderr.txt", "wb") as err:
            os.chmod(folder / "stdout.txt", 0o600)
            os.chmod(folder / "stderr.txt", 0o600)
            proc = subprocess.run(command, cwd=cwd, shell=False, stdout=out, stderr=err,
                                  timeout=limit, check=False)
        status.update(status="completed", exit_code=proc.returncode)
    except subprocess.TimeoutExpired:
        status.update(status="timeout", exit_code=None)
    except Exception:
        status.update(status="failed", exit_code=None, error="execution failed")
    status["finished_at"] = time.time()
    _atomic(folder / "status.json", status)

if __name__ == "__main__":
    main(sys.argv[1])
