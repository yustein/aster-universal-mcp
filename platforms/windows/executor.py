"""Windows command executor, standalone local beta (no network listener)."""
import os
import subprocess
import time
import uuid
from pathlib import Path

MAX_OUTPUT = 65536

def run_command(command: list[str], cwd: str = ".", timeout: int = 30) -> dict:
    request_id = str(uuid.uuid4())
    if (not isinstance(command, list) or not command or len(command) > 64
            or any(not isinstance(x, str) or len(x) > 8192 for x in command)):
        return {"status": "rejected", "request_id": request_id, "error": "invalid command"}
    if not isinstance(timeout, int) or not 1 <= timeout <= 300:
        return {"status": "rejected", "request_id": request_id, "error": "invalid timeout"}
    path = Path(cwd).resolve()
    if not path.is_dir():
        return {"status": "rejected", "request_id": request_id, "error": "invalid cwd"}
    try:
        completed = subprocess.run(command, cwd=str(path), shell=False,
                                   capture_output=True, timeout=timeout, check=False)
        return {
            "status": "completed", "request_id": request_id,
            "exit_code": completed.returncode,
            "stdout": completed.stdout[:MAX_OUTPUT].decode("utf-8", "replace"),
            "stderr": completed.stderr[:MAX_OUTPUT].decode("utf-8", "replace"),
            "stdout_truncated": len(completed.stdout) > MAX_OUTPUT,
            "stderr_truncated": len(completed.stderr) > MAX_OUTPUT,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "request_id": request_id, "exit_code": None}
    except OSError:
        return {"status": "error", "request_id": request_id, "error": "process launch failed"}
