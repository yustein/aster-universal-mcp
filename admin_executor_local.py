"""Local root executor. Root-only Unix socket; no network listener."""
import json
import os
import socket
import socketserver
import struct
import stat
import time
import uuid
from pathlib import Path
from admin_jobs import start_job, job_status, cancel_job, run_command, reconcile_after_boot
import file_transfer

SOCKET_PATH = os.environ.get("ASTER_ADMIN_SOCKET", "/run/aster-mcp-admin.sock")
AUDIT_PATH = os.environ.get("ASTER_ADMIN_AUDIT", "/var/log/aster-mcp-admin.jsonl")
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
    cwd = request.get("cwd", os.environ.get("ASTER_WORKSPACE", "/var/lib/aster-universal-mcp/workspace"))
    validate(command, cwd)
    if operation == "start_job":
        return start_job(command, cwd, request.get("max_runtime", 3600))
    if operation != "run":
        raise ValueError("unknown operation")
    timeout = request.get("timeout", 5)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise ValueError("timeout must be 1..300 seconds")
    return run_command(command, cwd, timeout)

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request_id = str(uuid.uuid4())
        request = {}
        response = {"status": "rejected", "exit_code": None}
        try:
            self.request.settimeout(5)
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
        if not isinstance(request, dict):
            request = {}
        response["request_id"] = request_id
        # Never log argv, cwd, payload, stdout, stderr, exception text or secrets.
        entry = {"timestamp": time.time(), "request_id": request_id,
                 "operation": request.get("operation") if request.get("operation") in ("run", "start_job", "job_status", "cancel_job", "begin_upload", "upload_chunk", "finish_upload", "download_info", "download_chunk") else "invalid",
                 "status": response["status"], "exit_code": response.get("exit_code"),
                 "job_id": response.get("job_id") if response.get("job_id") else None}
        _audit(entry)
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())

class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

def _protected_parent(path):
    path = Path(path)
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise PermissionError("canonical absolute control path required")
    for parent in (path.parent, *path.parent.parents):
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError("root-protected control ancestors required")
    return path


def _audit(entry):
    fd = os.open(AUDIT_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise PermissionError("protected audit file required")
        raw = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
        os.write(fd, raw)
    finally:
        os.close(fd)


def main():
    if os.geteuid() != 0:
        raise PermissionError("root executor required")
    os.umask(0o077)
    path = _protected_parent(SOCKET_PATH)
    _protected_parent(AUDIT_PATH)
    if path.exists() or path.is_symlink():
        original = path.lstat()
        if not stat.S_ISSOCK(original.st_mode) or original.st_uid != 0 or original.st_mode & 0o077:
            raise PermissionError("unexpected administrator socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(SOCKET_PATH)
            except ConnectionRefusedError:
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    raise RuntimeError("administrator socket changed")
                path.unlink()
            else:
                raise RuntimeError("administrator socket already active")
    _audit({"timestamp": time.time(), "operation": "executor_start", "status": "starting"})
    reconcile_after_boot()
    with Server(SOCKET_PATH, Handler) as server:
        os.chmod(SOCKET_PATH, 0o600)
        created = path.lstat()
        try:
            server.serve_forever()
        finally:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass

if __name__ == "__main__":
    main()
