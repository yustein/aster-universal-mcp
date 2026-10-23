"""Independent Windows OAuth MCP gateway, bound to loopback only.

Does not reuse Amsterdam credentials, OAuth database, or administrative socket.
"""
import os
import sys
from pathlib import Path

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from oauth_runtime import build_oauth_server
from mcp.server.auth.middleware.auth_context import get_access_token

def create_app():
    origin = os.environ["ASTER_WINDOWS_PUBLIC_ORIGIN"]
    os.environ["ASTER_MCP_PUBLIC_ORIGIN"] = origin
    database = Path(os.environ["ASTER_WINDOWS_OAUTH_DB"])
    if not database.is_absolute():
        raise ValueError("absolute database path required")
    database.parent.mkdir(parents=True, exist_ok=True)
    from executor import run_command
    from job_launcher import start_job
    from jobs import job_status
    from job_cancel import cancel_job
    from transfer import begin_upload, upload_chunk
    from upload_finish import finish_upload
    from download import download_info, download_chunk

    def register(mcp):
        def authenticated():
            token = get_access_token()
            if token is None:
                raise PermissionError("authentication required")
            if "system:admin" not in token.scopes:
                raise PermissionError("administrator OAuth scope required")
            import json
            allowlist_path = Path(os.environ["ASTER_WINDOWS_ADMIN_CLIENTS"])
            allowed = json.loads(allowlist_path.read_text(encoding="utf-8-sig"))
            if not isinstance(allowed, list) or token.client_id not in allowed:
                raise PermissionError("administrator client not authorized")
            return token
        @mcp.tool()
        def list_directory(path: str = ".") -> dict:
            authenticated()
            folder = Path(path).resolve()
            if not folder.is_dir():
                raise ValueError("not a directory")
            return {"path": str(folder), "entries": [p.name for p in list(folder.iterdir())[:100]]}
        @mcp.tool()
        def execute_command(command: list[str], cwd: str = ".", timeout: int = 30) -> dict:
            authenticated()
            return run_command(command, cwd, timeout)
        @mcp.tool()
        def start_background_job(command: list[str], cwd: str = ".", max_runtime: int = 3600) -> dict:
            authenticated()
            return start_job(command, cwd, max_runtime)
        @mcp.tool()
        def background_job_status(job_id: str) -> dict:
            authenticated()
            return job_status(job_id)
        @mcp.tool()
        def cancel_background_job(job_id: str) -> dict:
            authenticated()
            return cancel_job(job_id)
        @mcp.tool()
        def begin_file_upload(destination: str, size: int, sha256: str) -> dict:
            authenticated()
            return begin_upload(destination, size, sha256)
        @mcp.tool()
        def append_file_chunk(transfer_id: str, offset: int, data_base64: str) -> dict:
            authenticated()
            return upload_chunk(transfer_id, offset, data_base64)
        @mcp.tool()
        def complete_file_upload(transfer_id: str) -> dict:
            authenticated()
            return finish_upload(transfer_id)
        @mcp.tool()
        def inspect_file(path: str) -> dict:
            authenticated()
            return download_info(path)
        @mcp.tool()
        def read_file_chunk(path: str, offset: int, limit: int = 16384) -> dict:
            authenticated()
            return download_chunk(path, offset, limit)
    server = build_oauth_server(db_path=str(database), origin=origin, register_tools=register)
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[origin.removeprefix("https://")], allowed_origins=[origin]))
    # Match Linux: only trust a locally connected reverse proxy claiming the
    # configured HTTPS hostname. Never trust arbitrary client-supplied headers.
    from proxy_security import PublicOriginGate
    return PublicOriginGate(app, public_origin=origin)

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1",
                port=int(os.environ.get("ASTER_WINDOWS_PORT", "18767")),
                proxy_headers=False, access_log=False)
