"""Root-local Unix-socket client for administrator executor."""
import json
import os
import socket

SOCKET_PATH = os.environ.get("ASTER_ADMIN_SOCKET", "/run/aster-mcp-admin.sock")

def _request(payload, wait=15):
    raw = json.dumps(payload).encode("utf-8") + b"\n"
    if len(raw) > 32768:
        return {"status": "rejected", "error": "request too large"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(wait)
            client.connect(SOCKET_PATH)
            client.sendall(raw)
            response = bytearray()
            while not response.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > 262144:
                    raise ValueError("executor response too large")
        return json.loads(response)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "error", "error": "executor communication failed", "exit_code": None}

def run_admin(command: list[str], cwd: str = "/", timeout: int = 5):
    if type(timeout) is not int or not 1 <= timeout <= 300:
        return {"status": "rejected", "error": "timeout must be 1..300 seconds"}
    return _request({"operation": "run", "command": command, "cwd": cwd, "timeout": timeout}, timeout + 35)

def start_admin_job(command: list[str], cwd: str = "/", max_runtime: int = 3600):
    return _request({"operation": "start_job", "command": command, "cwd": cwd, "max_runtime": max_runtime})

def get_admin_job(job_id: str):
    return _request({"operation": "job_status", "job_id": job_id})


def cancel_admin_job(job_id: str):
    return _request({"operation": "cancel_job", "job_id": job_id}, wait=25)

def begin_upload(destination, size, sha256):
    return _request({"operation":"begin_upload","destination":destination,"size":size,"sha256":sha256})
def upload_chunk(transfer_id, offset, data_base64):
    return _request({"operation":"upload_chunk","transfer_id":transfer_id,"offset":offset,"data_base64":data_base64})
def finish_upload(transfer_id):
    return _request({"operation":"finish_upload","transfer_id":transfer_id})
def download_info(path):
    return _request({"operation":"download_info","path":path})
def download_chunk(path, offset, limit=16384):
    return _request({"operation":"download_chunk","path":path,"offset":offset,"limit":limit})
