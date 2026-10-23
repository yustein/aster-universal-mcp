"""M2 restricted command executor prototype. No shell or privilege escalation."""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

ALLOWED = {("ls", "-la")}
MAX_OUTPUT = 16384

def run_restricted(command: list[str], cwd: str, root: str, timeout: int = 5, audit_path: str | None = None) -> dict:
    """Execute a single allowlisted argv vector within a confined directory."""
    request_id = str(uuid.uuid4())
    started = time.time()
    result = {"request_id": request_id, "stdout": "", "stderr": "", "exit_code": None}
    try:
        if not isinstance(command, list) or any(not isinstance(s, str) for s in command):
            raise ValueError("command must be a string argv list")
        write_text = len(command) == 3 and command[0] == "write-text"
        if not write_text and tuple(command) not in ALLOWED:
            raise PermissionError("command not allowlisted")
        if not isinstance(timeout, int) or not 1 <= timeout <= 10:
            raise ValueError("timeout outside 1..10 seconds")
        base = Path(root).resolve(strict=True)
        target = (base / cwd).resolve(strict=True)
        if not target.is_relative_to(base) or not target.is_dir():
            raise PermissionError("working directory outside authorized root")
        if write_text:
            filename, content = command[1], command[2]
            if not filename or filename in (".", "..") or "/" in filename or "\\\\" in filename or len(content.encode("utf-8")) > 16384:
                raise ValueError("invalid filename or content size")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(target / filename), flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            result.update(stdout=f"created {filename}", exit_code=0)
            status = "completed"
        else:
            if os.name == "nt":
                # The only allowlisted read command is ls -la. Clean Windows
                # does not ship ls, so enumerate safely without a shell.
                from filesystem import list_directory
                listing = list_directory(".", root=str(target), limit=200)
                lines = [f"{entry['mode']} {entry['size']:>10} {entry['name']}"
                         for entry in listing["entries"]]
                if listing["truncated"]:
                    lines.append("[directory listing truncated at 200 entries]")
                payload = "\n".join(lines).encode("utf-8")
                result.update(stdout=payload[:MAX_OUTPUT].decode("utf-8", "replace"),
                              stderr="", exit_code=0,
                              stdout_truncated=len(payload) > MAX_OUTPUT,
                              stderr_truncated=False)
            else:
                completed = subprocess.run(command, cwd=str(target), shell=False, capture_output=True,
                                           timeout=timeout, check=False)
                result.update(stdout=completed.stdout[:MAX_OUTPUT].decode("utf-8", "replace"),
                              stderr=completed.stderr[:MAX_OUTPUT].decode("utf-8", "replace"),
                              exit_code=completed.returncode,
                              stdout_truncated=len(completed.stdout) > MAX_OUTPUT,
                              stderr_truncated=len(completed.stderr) > MAX_OUTPUT)
            status = "completed"
    except subprocess.TimeoutExpired:
        status = "timeout"
        result["error"] = "execution timed out"
    except (ValueError, PermissionError, OSError) as exc:
        status = "rejected"
        result["error"] = str(exc)
    result["status"] = status
    if audit_path is not None:
        entry = {"request_id": request_id, "timestamp": started, "argv": (["write-text", command[1], "<content redacted>"] if len(command) == 3 and command[0] == "write-text" else command),
                 "cwd": cwd, "status": status, "exit_code": result["exit_code"]}
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return result
