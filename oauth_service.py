"""Authenticated MCP application. Loopback-only behind a trusted TLS proxy."""
import os
import json
from mcp.server.auth.middleware.auth_context import get_access_token
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from oauth_runtime import build_oauth_server
from filesystem import list_directory as enumerate_directory
from admin_client import run_admin, start_admin_job, get_admin_job, cancel_admin_job, begin_upload as admin_begin_upload, upload_chunk as admin_upload_chunk, finish_upload as admin_finish_upload, download_info as admin_download_info, download_chunk as admin_download_chunk
from proxy_security import PublicOriginGate
from oauth_http_diagnostics import OAuthHTTPDiagnostics

def create_app():
    origin=os.environ["ASTER_MCP_PUBLIC_ORIGIN"]
    db=os.environ["ASTER_MCP_OAUTH_DB"]
    root=os.environ["ASTER_ALLOWED_ROOT"]
    if not os.path.isabs(db) or not os.path.isabs(root):
        raise ValueError("absolute paths required")
    if not os.path.isdir(root):
        raise ValueError("allowed root missing")
    def require_admin():
        token = get_access_token()
        if token is None:
            raise PermissionError("authenticated client required")
        if "system:admin" not in token.scopes:
            raise PermissionError("administrator OAuth scope required")
        with open(os.environ.get("ASTER_ADMIN_CLIENTS", "/var/lib/aster-universal-mcp/admin-client-allowlist.json"), encoding="utf-8-sig") as f:
            allowed = json.load(f)
        if token.client_id not in allowed:
            raise PermissionError("administrator client not authorized")
    def register(server):
        @server.tool()
        def list_directory(path: str=".", limit: int=100)->dict:
            return enumerate_directory(path,root=root,limit=limit)
        @server.tool()
        def run_command(command: list[str], cwd: str = ".", timeout: int = 5) -> dict:
            """Execute commands through the local administrator executor with audit. Use an absolute cwd for full server access."""
            require_admin()
            return run_admin(command, root if cwd == "." else cwd, timeout)
        @server.tool()
        def start_job(command: list[str], cwd: str = "/", max_runtime: int = 3600) -> dict:
            """Start a background server job and return a job ID. Maximum runtime 86400 seconds."""
            require_admin()
            return start_admin_job(command, root if cwd == "." else cwd, max_runtime)
        @server.tool()
        def job_status(job_id: str) -> dict:
            """Get durable job status, exit code, and bounded output by job ID."""
            require_admin()
            return get_admin_job(job_id)
        @server.tool()
        def cancel_job(job_id: str) -> dict:
            """Cancel a running background job by its ID."""
            require_admin()
            return cancel_admin_job(job_id)
        @server.tool()
        def begin_upload(destination: str, size: int, sha256: str) -> dict:
            """Begin resumable SHA256-verified upload to a new absolute path."""
            require_admin()
            return admin_begin_upload(destination, size, sha256)
        @server.tool()
        def upload_chunk(transfer_id: str, offset: int, data_base64: str) -> dict:
            """Append base64 chunk to an upload, with strict sequential offset."""
            require_admin()
            return admin_upload_chunk(transfer_id, offset, data_base64)
        @server.tool()
        def finish_upload(transfer_id: str) -> dict:
            """Verify upload SHA256 and atomically publish a new file."""
            require_admin()
            return admin_finish_upload(transfer_id)
        @server.tool()
        def download_info(path: str) -> dict:
            """Return remote file size and SHA256."""
            require_admin()
            return admin_download_info(path)
        @server.tool()
        def download_chunk(path: str, offset: int, limit: int = 16384) -> dict:
            """Download a bounded base64 chunk at a byte offset."""
            require_admin()
            return admin_download_chunk(path, offset, limit)

    server=build_oauth_server(db_path=db,origin=origin,register_tools=register)
    host=origin.removeprefix("https://")
    app=server.streamable_http_app(
        stateless_http=True,json_response=True,host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[host],allowed_origins=[origin]))
    return OAuthHTTPDiagnostics(PublicOriginGate(app,public_origin=origin))

def main():
    uvicorn.run(create_app(),host="127.0.0.1",
                port=int(os.environ.get("ASTER_MCP_PORT","8767")),
                proxy_headers=False,access_log=False)

if __name__=="__main__":
    main()
