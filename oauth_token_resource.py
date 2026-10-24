"""Bind explicitly requested token resources before the SDK consumes a grant.

MCP SDK 2.3.0 validates stored grant audiences but does not validate a supplied
resource field during code/refresh exchange. Preserve its token handling while
rejecting a mismatched request early. Omitted resource remains compatible with
clients renewing the single already-bound resource.
"""
import json
from urllib.parse import parse_qs


MAX_TOKEN_FORM_BYTES = 65536


class TokenResourceBinding:
    def __init__(self, app, *, resource):
        self.app = app
        self.resource = resource

    async def reject(self, send, error, status=400):
        # Never echo a submitted resource, credential, request body or parser
        # exception. The outer HTTP diagnostics adds its safe request ID.
        body = json.dumps({"error": error}, separators=(",", ":")).encode("ascii")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"), (b"pragma", b"no-cache"),
        ]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("path") != "/token"
                or scope.get("method") != "POST"):
            return await self.app(scope, receive, send)
        content_types = [value for key, value in scope.get("headers", [])
                         if key.lower() == b"content-type"]
        if (len(content_types) != 1 or
                content_types[0].split(b";", 1)[0].strip().lower()
                != b"application/x-www-form-urlencoded"):
            return await self.reject(send, "invalid_request")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                return await self.reject(send, "invalid_request")
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_TOKEN_FORM_BYTES:
                return await self.reject(send, "invalid_request", status=413)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True,
                              max_num_fields=128, errors="strict")
        except (ValueError, UnicodeError):
            return await self.reject(send, "invalid_request")
        resources = fields.get("resource")
        if resources is not None and resources != [self.resource]:
            return await self.reject(send, "invalid_target")
        original_body = bytes(body)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": original_body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)
