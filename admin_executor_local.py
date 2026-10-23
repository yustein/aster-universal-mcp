"""Local root executor. Root-only Unix socket; no network listener."""
import json
import os
import socket
import socketserver
import struct
import subprocess
import time
import uuid
from pathlib import Path
from admin_jobs import start_job, job_status, cancel_job
import file_transfer

SOCKET_PATH = "/run/aster-mcp-admin.sock"
AUDIT_PATH = "/var/log/aster-mcp-admin.jsonl"
MAX_REQUEST = 32768
MAX_OUTPUT = 16384

def validate(command, cwd):
    if (not isinstance(command, list) or not command or len(command) > 64
            or any(not isinstance(x, str) or len(x) > 8192 for x in command)):
        raise ValueError("invalid argv")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError("absolute existing working directory required")

def execute(request):
    operation = request.get("operation", "run")
    if operation == "begin_upload":
        return file_transfer.begin_upload(request["destination"], request["size"], request["sha256"])
    if operation == "upload_chunk":
        return file_transfer.upload_chunk(request["transfer_id"], request["offset"], request["data_base64"])
    if operation == "finish_upload":
        return file_transfer.finish_upload(request["transfer_id"])
    if operation == "download_info":
        return file_transfer.download_info(request["path"])
    if operation == "download_chunk":
        return file_transfer.download_chunk(request["path"], request["offset"], request.get("limit", 16384))
    if operation == "job_status":
        return job_status(request["job_id"])
    if operation == "cancel_job":
        return cancel_job(request["job_id"])
    command = request.get("command")
    cwd = request.get("cwd", "/")
    validate(command, cwd)
    if operation == "start_job":
        return start_job(command, cwd, request.get("max_runtime", 3600))
    if operation != "run":
        raise ValueError("unknown operation")
    timeout = request.get("timeout", 5)
    if not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ValueError("timeout must be 1..300 seconds")
    try:
        completed = subprocess.run(command, cwd=cwd, shell=False, capture_output=True,
                                   timeout=timeout, check=False)
        return {"status": "completed", "exit_code": completed.returncode,
                "stdout": completed.stdout[:MAX_OUTPUT].decode("utf-8", "replace"),
                "stderr": completed.stderr[:MAX_OUTPUT].decode("utf-8", "replace"),
                "stdout_truncated": len(completed.stdout) > MAX_OUTPUT,
                "stderr_truncated": len(completed.stderr) > MAX_OUTPUT}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "exit_code": None, "stdout": "", "stderr": "timeout"}

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request_id = str(uuid.uuid4())
        request = {}
        response = {"status": "rejected", "exit_code": None}
        try:
            peer = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            pid, uid, gid = struct.unpack("3i", peer)
            if uid != 0:
                raise PermissionError("root-local peer required")
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST:
                raise ValueError("request too large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be object")
            response = execute(request)
            response.setdefault("status", "completed")
        except (ValueError, KeyError, TypeError, PermissionError, OSError) as exc:
            response = {"status": "rejected", "exit_code": None, "error": type(exc).__name__}
        response["request_id"] = request_id
        # Never log argv, cwd, payload, stdout, stderr, exception text or secrets.
        entry = {"timestamp": time.time(), "request_id": request_id,
                 "operation": request.get("operation") if request.get("operation") in ("run", "start_job", "job_status", "cancel_job", "begin_upload", "upload_chunk", "finish_upload", "download_info", "download_chunk") else "invalid",
                 "status": response["status"], "exit_code": response.get("exit_code"),
                 "job_id": response.get("job_id") if response.get("job_id") else None}
        with open(AUDIT_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        self.wfile.write((json.dumps(response) + "\n").encode())

class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

def main():
    if os.path.exists(SOCKET_PATH):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(SOCKET_PATH)
            except ConnectionRefusedError:
                os.unlink(SOCKET_PATH)
            else:
                raise RuntimeError("administrator socket already active")
    with Server(SOCKET_PATH, Handler) as server:
        os.chmod(SOCKET_PATH, 0o600)
        try:
            server.serve_forever()
        finally:
            os.unlink(SOCKET_PATH)

if __name__ == "__main__":
    main()
