"""Fail-closed HTTP bearer gate for MCP requests.

This ASGI middleware is an additional defensive boundary. The SDK OAuth
resource-server integration is still required for production compatibility.
"""
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

class BearerGate:
    def __init__(self, app, *, token_store, resource: str, scope: str = "system:read"):
        self.app = app
        self.token_store = token_store
        self.resource = resource
        self.scope = scope

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await JSONResponse({"error": "websocket_disabled"}, status_code=403)(scope, receive, send)
            return
        path = scope.get("path", "")
        if path != "/mcp" and not path.startswith("/mcp/"):
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization", "")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            await JSONResponse({"error": "unauthorized"}, status_code=401,
                               headers={"WWW-Authenticate": 'Bearer realm="aster-mcp"'})(scope, receive, send)
            return
        client = self.token_store.validate(parts[1], resource=self.resource, scope=self.scope)
        if client is None:
            await JSONResponse({"error": "invalid_token"}, status_code=401,
                               headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})(scope, receive, send)
            return
        scope["aster_client_id"] = client
        await self.app(scope, receive, send)
