"""Fail-closed MCP HTTP runtime using the existing ASGI bearer gate.

Local integration step only. A standards-compliant OAuth provider and HTTPS
client pairing must be completed before Caddy exposes this service.
"""
import os
import uvicorn
from bearer_gate import BearerGate
from token_store import TokenStore

def build_gated_app(server, *, db_path: str, resource: str):
    if not resource.startswith("https://") or not resource.endswith("/mcp"):
        raise ValueError("canonical HTTPS MCP resource required")
    if not db_path:
        raise ValueError("token database path required")
    app = server.streamable_http_app(
        streamable_http_path="/mcp", host="127.0.0.1",
        stateless_http=True, json_response=True,
    )
    return BearerGate(app, token_store=TokenStore(db_path),
                      resource=resource, scope="system:read")

def run_gated(server):
    db_path = os.environ.get("ASTER_MCP_TOKEN_DB")
    resource = os.environ.get("ASTER_MCP_RESOURCE")
    if not db_path or not resource:
        raise RuntimeError("refusing to start without token database and resource")
    app = build_gated_app(server, db_path=db_path, resource=resource)
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("ASTER_MCP_PORT", "8765")),
                log_level="info")
