"""Optional standalone coordination companion, with Aster's existing OAuth flow.

This process exposes coordination tools only. It does not import a machine
executor, share PA state or start a privileged machine job. Native Events are an
additional explicit opt-in. Use a distinct origin, OAuth DB and allowlist.
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from oauth_runtime import build_oauth_server
from oauth_http_diagnostics import OAuthHTTPDiagnostics
from proxy_security import PublicOriginGate
from tool_access import current_client_access
from .tools import register_coordination_tools
from .panel import build_panel_app


def create_app():
    origin = os.environ["ASTER_MCP_PUBLIC_ORIGIN"]
    oauth_db = Path(os.environ["ASTER_MCP_OAUTH_DB"])
    coordination_db = Path(os.environ["ASTER_COORDINATION_DB"])
    allowlist = Path(os.environ["ASTER_ADMIN_CLIENTS"])
    native_flag = os.environ.get("ASTER_COORDINATION_NATIVE_EVENTS", "0")
    if native_flag not in ("0", "1"):
        raise ValueError("ASTER_COORDINATION_NATIVE_EVENTS must be 0 or 1")
    if not all(path.is_absolute() for path in (oauth_db, coordination_db, allowlist)):
        raise ValueError("all companion state paths must be absolute")
    if len({path.resolve() for path in (oauth_db, coordination_db, allowlist)}) != 3:
        raise ValueError("coordination, OAuth and allowlist must be separate state files")
    stores = {}
    panel, register_panel_tools = build_panel_app(lambda: stores["coordination"], allowlist_path=allowlist)
    def register(server):
        @server.tool()
        def connection_status() -> dict:
            """Show this companion client's identity and approval without granting access or executing machine commands."""
            access = current_client_access(allowlist)
            return {"authenticated": True, "client_id": access.client_id,
                    "admin_approved": access.admin_approved, "service": "coordination_companion",
                    "machine_execution": False,
                    "next_step": "The local operator must approve this exact client and enroll it with coordination.cli; then call aster_profile."}
        store = register_coordination_tools(server, db_path=str(coordination_db), allowlist_path=allowlist)
        stores["coordination"] = store
        register_panel_tools(server)
        if native_flag == "1":
            from .native import NativeEvents, install_native_events
            install_native_events(server, NativeEvents(store, allowlist_path=allowlist, oauth_db_path=oauth_db))
    server = build_oauth_server(db_path=str(oauth_db), origin=origin, register_tools=register,
                                extensions=[panel])
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[origin.removeprefix("https://")], allowed_origins=[origin]))
    return OAuthHTTPDiagnostics(PublicOriginGate(app, public_origin=origin))


def main():
    import uvicorn
    # This is deliberately a companion port, not either released machine port.
    port = int(os.environ.get("ASTER_COORDINATION_PORT", "18769"))
    if not 1024 <= port <= 65535:
        raise ValueError("choose an unprivileged coordination port")
    uvicorn.run(create_app(), host="127.0.0.1", port=port,
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
