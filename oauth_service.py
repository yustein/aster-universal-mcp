"""Authenticated MCP application. Loopback-only behind a trusted TLS proxy."""
import os
from pathlib import Path
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.mcpserver.exceptions import ToolError
from oauth_runtime import build_oauth_server
from filesystem import DirectoryAccessDenied, list_directory as enumerate_directory
from tool_access import current_client_access, connection_status as describe_connection
from admin_client import run_admin, start_admin_job, get_admin_job, cancel_admin_job, begin_upload as admin_begin_upload, upload_chunk as admin_upload_chunk, finish_upload as admin_finish_upload, download_info as admin_download_info, download_chunk as admin_download_chunk
from proxy_security import PublicOriginGate
from oauth_http_diagnostics import OAuthHTTPDiagnostics

def create_app():
    origin=os.environ["ASTER_MCP_PUBLIC_ORIGIN"]
    db=os.environ["ASTER_MCP_OAUTH_DB"]
    root=os.environ["ASTER_ALLOWED_ROOT"]
    workspace=os.environ.get("ASTER_WORKSPACE", root)
    if not os.path.isabs(db) or not os.path.isabs(root) or not os.path.isabs(workspace):
        raise ValueError("absolute paths required")
    if not os.path.isdir(root) or not os.path.isdir(workspace):
        raise ValueError("allowed root or workspace missing")
    root, workspace = os.path.realpath(root), os.path.realpath(workspace)
    allowlist = os.environ.get("ASTER_ADMIN_CLIENTS", "/var/lib/aster-universal-mcp/admin-client-allowlist.json")
    coordination_db = os.environ.get("ASTER_COORDINATION_DB")
    native_events = os.environ.get("ASTER_COORDINATION_NATIVE_EVENTS")
    if native_events not in (None, "0", "1"):
        raise ValueError("ASTER_COORDINATION_NATIVE_EVENTS must be 0 or 1")
    if native_events == "1" and coordination_db is None:
        raise ValueError("native coordination events require ASTER_COORDINATION_DB")
    if coordination_db is not None:
        if not coordination_db or not Path(coordination_db).is_absolute():
            raise ValueError("ASTER_COORDINATION_DB must be an absolute database path")
        if Path(coordination_db).resolve() == Path(db).resolve():
            raise ValueError("coordination and OAuth databases must be separate")
    def require_admin():
        current_client_access(allowlist).require_admin()
    def register(server):
        @server.tool()
        def connection_status() -> dict:
            """Show this authenticated client's administrative approval and path policy; never grants access."""
            return describe_connection(current_client_access(allowlist), platform="linux",
                                       default_workspace=workspace, listing_base=root)
        @server.tool()
        def list_directory(path: str=".", limit: int=100)->dict:
            """List from the configured listing root; an approved client may use absolute host paths. Bounded sample, unknown total if truncated."""
            access = current_client_access(allowlist)
            unconfined = access.admin_approved and Path(path).is_absolute()
            try:
                return enumerate_directory(path, root=None if unconfined else root,
                                           base=root, limit=limit)
            except DirectoryAccessDenied:
                if not access.admin_approved:
                    raise access.approval_required() from None
                raise ToolError(
                    "LISTING_PATH_OUTSIDE_ROOT: Relative listing paths stay within "
                    "the configured listing root. This approved client may use an "
                    "explicit absolute path for a directory elsewhere on the host."
                ) from None
        @server.tool()
        def run_command(command: list[str], cwd: str = ".", timeout: int = 5) -> dict:
            """Execute commands through the local administrator executor with audit. Use an absolute cwd for full server access."""
            require_admin()
            return run_admin(command, workspace if cwd == "." else cwd, timeout)
        @server.tool()
        def start_job(command: list[str], cwd: str = ".", max_runtime: int = 3600) -> dict:
            """Start a background server job and return a job ID. Maximum runtime 86400 seconds."""
            require_admin()
            return start_admin_job(command, workspace if cwd == "." else cwd, max_runtime)
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

        if coordination_db is not None:
            from coordination.tools import register_coordination_tools
            store = register_coordination_tools(server, db_path=coordination_db,
                                                allowlist_path=allowlist)
            if native_events == "1":
                from coordination.native import NativeEvents, install_native_events
                install_native_events(server, NativeEvents(store, allowlist_path=allowlist,
                                                          oauth_db_path=db))

    server=build_oauth_server(db_path=db,origin=origin,register_tools=register)
    host=origin.removeprefix("https://")
    app=server.streamable_http_app(
        stateless_http=True,json_response=True,host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[host],allowed_origins=[origin]))
    return OAuthHTTPDiagnostics(PublicOriginGate(app,public_origin=origin))

def main():
    os.chdir(os.environ.get("ASTER_WORKSPACE", os.environ["ASTER_ALLOWED_ROOT"]))
    uvicorn.run(create_app(),host="127.0.0.1",
                port=int(os.environ.get("ASTER_MCP_PORT","8767")),
                proxy_headers=False,access_log=False)

if __name__=="__main__":
    main()
