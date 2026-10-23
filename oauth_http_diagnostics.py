"""Secret-free HTTP status telemetry for OAuth endpoints."""
import logging
import time

log = logging.getLogger("aster.oauth.http")
TRACK = {"/register", "/authorize", "/token", "/pair/redeem", "/pair/complete"}

class OAuthHTTPDiagnostics:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in TRACK:
            return await self.app(scope, receive, send)
        path = scope["path"]
        method = scope.get("method", "")
        start = time.monotonic()
        status = [0]
        async def observed(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, observed)
        finally:
            log.info("oauth endpoint=%s method=%s status=%d duration_ms=%d",
                     path, method, status[0], int((time.monotonic()-start)*1000))
