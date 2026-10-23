"""Explicit public-origin enforcement behind a TLS-terminating proxy.

Do not infer the public origin from arbitrary forwarded headers.
"""
from starlette.responses import JSONResponse

class PublicOriginGate:
    def __init__(self, app, *, public_origin: str, trusted_proxy_host: str = "127.0.0.1"):
        if not public_origin.startswith("https://") or public_origin.endswith("/"):
            raise ValueError("canonical HTTPS origin required")
        self.app=app
        self.public_origin=public_origin
        self.trusted_proxy_host=trusted_proxy_host

    async def __call__(self, scope, receive, send):
        if scope["type"]!="http":
            await self.app(scope,receive,send)
            return
        headers={k.decode("latin-1").lower():v.decode("latin-1") for k,v in scope.get("headers",[])}
        host=headers.get("host","")
        forwarded=headers.get("x-forwarded-proto","")
        client=(scope.get("client") or ("",0))[0]
        # Only a trusted loopback reverse proxy may supply HTTPS forwarding.
        if client!=self.trusted_proxy_host or forwarded!="https" or host!=self.public_origin[8:]:
            await JSONResponse({"error":"untrusted_proxy_origin"},status_code=403)(scope,receive,send)
            return
        scope=dict(scope)
        scope["scheme"]="https"
        await self.app(scope,receive,send)
