"""Secret-free HTTP status telemetry for OAuth endpoints."""
import logging
import secrets
import time

log = logging.getLogger("aster.oauth.http")
TRACK = {"/register", "/authorize", "/token", "/pair/authorize", "/pair/redeem",
         "/pair/complete", "/mcp", "/.well-known/oauth-authorization-server",
         "/.well-known/oauth-protected-resource/mcp"}

class OAuthHTTPDiagnostics:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in TRACK:
            return await self.app(scope, receive, send)
        path = scope["path"]
        method = scope.get("method", "")
        request_id = secrets.token_hex(12)
        start = time.monotonic()
        status = [0]
        async def observed(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-aster-request-id", request_id.encode("ascii"))]
            await send(message)
        try:
            await self.app(scope, receive, observed)
        finally:
            log.info("oauth request_id=%s endpoint=%s method=%s status=%d duration_ms=%d",
                     request_id, path, method, status[0], int((time.monotonic()-start)*1000))
