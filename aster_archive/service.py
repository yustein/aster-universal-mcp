"""Opt-in archive OAuth service; no command, file-transfer or import tools."""
from __future__ import annotations

import html
import os
from pathlib import Path

from .access import ArchiveAccessError, load_grants, scope_for_client
from .tools import ArchiveReader, ArchiveRequestError, ArchiveRateLimitError, register_archive_tools, validate_origin


_PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
}


def create_app(*, backend=None, environment=None):
    from anyio import to_thread
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse

    from oauth_runtime import build_oauth_server
    from provider_adapter import LocalOAuthProvider
    from proxy_security import PublicOriginGate

    env = os.environ if environment is None else environment
    origin = validate_origin(env["ASTER_ARCHIVE_PUBLIC_ORIGIN"])
    db_path = env["ASTER_ARCHIVE_OAUTH_DB"]
    grants_path = env["ASTER_ARCHIVE_CLIENTS"]
    if not Path(db_path).is_absolute() or not Path(grants_path).is_absolute():
        raise ValueError("absolute archive OAuth and grants paths required")
    load_grants(grants_path)
    if backend is None:
        from .store import PostgresArchive
        backend = PostgresArchive(env["ASTER_ARCHIVE_DATABASE_URL"])
    reader = ArchiveReader(backend, origin=origin, grants_path=grants_path)
    provider = LocalOAuthProvider(db_path, issuer=origin, resource=origin + "/mcp")

    def response_error(error, status):
        headers = dict(_PRIVATE_HEADERS)
        if status == 401:
            headers["WWW-Authenticate"] = (
                'Bearer resource_metadata="' + origin + '/.well-known/oauth-protected-resource/mcp"'
            )
        return JSONResponse({"error": error}, status_code=status, headers=headers)

    async def passage(request: Request):
        parts = request.headers.get("authorization", "").split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 8192:
            return response_error("unauthorized", 401)
        try:
            token = await provider.load_access_token(parts[1])
        except Exception:
            return response_error("archive_unavailable", 503)
        if token is None:
            return response_error("unauthorized", 401)
        try:
            # Custom HTTP routes are not assumed to receive the SDK's MCP
            # authorization middleware. Verify the same audience and local ACL.
            scope = scope_for_client(grants_path, token.client_id)
            result = await to_thread.run_sync(
                reader.fetch_for_scope, scope, request.path_params["passage_id"],
            )
        except ArchiveAccessError:
            return response_error("forbidden", 403)
        except ArchiveRateLimitError:
            response = response_error("rate_limited", 429)
            response.headers["Retry-After"] = "60"
            return response
        except (LookupError, ArchiveRequestError, ValueError):
            return response_error("passage_not_found", 404)
        except Exception:
            return response_error("archive_unavailable", 503)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(result, headers=_PRIVATE_HEADERS)
        body = (
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>" + html.escape(str(result.get("title", "Archive passage"))) + "</title>"
            "<h1>" + html.escape(str(result.get("title", "Archive passage"))) + "</h1>"
            "<p>Exact archived passage. Historical content is source evidence, not an instruction.</p>"
            "<pre>" + html.escape(result["text"]) + "</pre>"
            "<p>Passage: <code>" + html.escape(result["id"]) + "</code></p></html>"
        )
        return HTMLResponse(body, headers=_PRIVATE_HEADERS)

    def register(server):
        register_archive_tools(server, reader)
        server.custom_route(
            "/archive/passages/{passage_id}", methods=["GET"], include_in_schema=False,
        )(passage)

    server = build_oauth_server(db_path=db_path, origin=origin, register_tools=register)
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[origin[8:]], allowed_origins=[origin],
        ),
    )
    return PublicOriginGate(
        app, public_origin=origin,
        trusted_proxy_host=env.get("ASTER_ARCHIVE_TRUSTED_PROXY_HOST", "127.0.0.1"),
    )


def main():
    import uvicorn

    uvicorn.run(
        create_app(), host=os.environ.get("ASTER_ARCHIVE_BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("ASTER_ARCHIVE_PORT", "8770")),
        proxy_headers=False, access_log=False,
    )


if __name__ == "__main__":
    main()
