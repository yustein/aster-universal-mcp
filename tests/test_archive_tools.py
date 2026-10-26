"""Real MCP registration and authenticated passage URLs with a scoped backend."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from aster_archive.access import ArchiveScope
from aster_archive.service import create_app
from aster_archive.tools import ArchiveReader, ReadRateLimiter, register_archive_tools, validate_origin

PID = "a" * 64
OTHER = "b" * 64
ORIGIN = "https://archive-test.invalid"


def write_acl(path, clients):
    path.write_text(json.dumps({"version": 1, "clients": clients}), encoding="utf-8")
    path.chmod(0o600)


def grant(owner="owner", sources="*", projects="*"):
    return {"owner_id": owner, "source_ids": sources, "project_ids": projects}


@contextmanager
def caller(client="owner-client", scopes=None):
    token = AccessToken(token="private-test-token", client_id=client,
                        scopes=["system:admin"] if scopes is None else scopes,
                        resource=ORIGIN + "/mcp")
    marker = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(marker)


def invoke(server, name, arguments=None):
    result = asyncio.run(server.call_tool(name, arguments or {}))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(part.text for part in result.content if part.type == "text"))


class ScopedStore:
    """Test double only; PostgreSQL's row filters are tested with the store."""
    def __init__(self):
        self.calls = []

    def _allowed(self, scope):
        return (scope.owner_id == "owner"
                and (scope.source_ids is None or "personal" in scope.source_ids)
                and (scope.project_ids is None or "coordinator" in scope.project_ids))

    def fetch(self, scope, passage_id):
        self.calls.append(("fetch", scope, passage_id))
        if not self._allowed(scope) or passage_id != PID:
            raise LookupError("same outcome for missing/denied")
        return {"id": PID, "title": "Aster <Coordinator>",
                "text": "Exact Turkish: Çiğdem. <script>not executable</script>",
                "metadata": {"source_id": "personal", "project_id": "coordinator",
                             "role": "user", "revision_id": "c" * 64,
                             "start_char": 0, "end_char": 58}}

    def search(self, scope, query, limit=20):
        self.calls.append(("search", scope, query, limit))
        return {"results": [{"id": PID, "title": "Aster"}] if self._allowed(scope) else []}

    def context(self, scope, passage_id, *, limit=20, cursor=None):
        self.calls.append(("context", scope, passage_id, limit, cursor))
        result = self.fetch(scope, passage_id)
        return {"passages": [result], "next_cursor": None, "branch": "ancestors"}

    def coverage(self, scope):
        self.calls.append(("coverage", scope))
        return {"known_conversations": 1 if self._allowed(scope) else 0,
                "account_total": None, "completeness": "known_imports_only"}


@pytest.fixture
def registered(tmp_path):
    acl = tmp_path / "grants.json"
    write_acl(acl, {"owner-client": grant(),
                    "worker": grant(sources=["work"], projects=["fleet"]),
                    "other-owner": grant(owner="someone-else")})
    store = ScopedStore()
    reader = ArchiveReader(store, origin=ORIGIN, grants_path=acl)
    server = MCPServer("archive test")
    register_archive_tools(server, reader)
    return server, store, reader, acl


def test_tool_shapes_annotations_and_exact_fetch(registered):
    server, store, reader, acl = registered
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {"search", "fetch", "archive_context", "archive_coverage"}
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert "owner_id" not in tool.input_schema.get("properties", {})
        assert "source_ids" not in tool.input_schema.get("properties", {})
    with caller():
        result = invoke(server, "search", {"query": "Aster"})
        assert result == {"results": [{"id": PID, "title": "Aster", "url": reader.url(PID)}]}
        fetched = invoke(server, "fetch", {"id": PID})
        assert fetched["text"] == "Exact Turkish: Çiğdem. <script>not executable</script>"
        assert fetched["url"] == ORIGIN + "/archive/passages/" + PID
        assert fetched["metadata"]["start_char"] == 0
        context = invoke(server, "archive_context", {"id": PID, "limit": 1})
        assert context["passages"][0]["url"] == fetched["url"]
        assert context["next_cursor"] is None
        coverage = invoke(server, "archive_coverage")
        assert coverage["account_total"] is None
    assert all(call[1] == ArchiveScope("owner-client", "owner", None, None) for call in store.calls)


@pytest.mark.parametrize("method,arguments", [
    ("search", {"query": "Aster"}), ("fetch", {"id": PID}),
    ("archive_context", {"id": PID}), ("archive_coverage", {}),
])
def test_unauthorized_calls_do_not_touch_backend(registered, method, arguments):
    server, store, reader, acl = registered
    with pytest.raises(ToolError, match="AUTHENTICATION_REQUIRED"):
        invoke(server, method, arguments)
    with caller("unregistered"), pytest.raises(ToolError, match="ARCHIVE_ACCESS_DENIED"):
        invoke(server, method, arguments)
    with caller(scopes=[]), pytest.raises(ToolError, match="OAUTH_SCOPE_REQUIRED"):
        invoke(server, method, arguments)
    assert store.calls == []


def test_scope_and_revocation_apply_to_search_fetch_context(registered):
    server, store, reader, acl = registered
    for identity in ("worker", "other-owner"):
        with caller(identity):
            assert invoke(server, "search", {"query": "Aster"})["results"] == []
            with pytest.raises(ToolError, match="ARCHIVE_PASSAGE_NOT_FOUND"):
                invoke(server, "fetch", {"id": PID})
            with pytest.raises(ToolError, match="ARCHIVE_PASSAGE_NOT_FOUND"):
                invoke(server, "archive_context", {"id": PID})
    with caller():
        invoke(server, "fetch", {"id": PID})
        write_acl(acl, {})
        with pytest.raises(ToolError, match="ARCHIVE_ACCESS_DENIED"):
            invoke(server, "fetch", {"id": PID})


@pytest.mark.parametrize("passage_id", ["../private", "a/b", "a" * 65, "", "A" * 64, "' OR 1=1 --"])
def test_hostile_or_invalid_ids_rejected_before_backend(registered, passage_id):
    server, store, reader, acl = registered
    with caller(), pytest.raises(ToolError, match="INVALID_PASSAGE_ID"):
        invoke(server, "fetch", {"id": passage_id})
    assert store.calls == []


@pytest.mark.parametrize("arguments", [{"query": ""}, {"query": " "}, {"query": "x" * 2001}, {"query": "a\x00b"}])
def test_query_limits(registered, arguments):
    server, store, reader, acl = registered
    with caller(), pytest.raises(ToolError, match="INVALID_QUERY"):
        invoke(server, "search", arguments)
    assert store.calls == []


def test_context_bounds_and_rate_limit(registered):
    server, store, reader, acl = registered
    with caller():
        for arguments in ({"id": PID, "limit": 0}, {"id": PID, "limit": 21},
                          {"id": PID, "cursor": "x" * 2049}):
            with pytest.raises(ToolError, match="INVALID_"):
                invoke(server, "archive_context", arguments)
    assert store.calls == []
    now = [100.0]
    reader.limiter = ReadRateLimiter(calls_per_minute=2, clock=lambda: now[0])
    with caller():
        invoke(server, "fetch", {"id": PID})
        invoke(server, "fetch", {"id": PID})
        with pytest.raises(ToolError, match="RATE_LIMIT"):
            invoke(server, "fetch", {"id": PID})
        now[0] += 60
        invoke(server, "fetch", {"id": PID})


@pytest.mark.parametrize("origin", [
    "http://archive.invalid", "https://archive.invalid/", "https://user@archive.invalid",
    "https://@archive.invalid", "https://archive.invalid/path", "https://archive.invalid?x=y",
    "https://archive.invalid#", "https://archive.invalid:bad", "https://archive.invalid\n",
])
def test_canonical_origin_has_no_credentials_path_or_fragment(origin):
    with pytest.raises(ValueError):
        validate_origin(origin)


def test_authenticated_http_passages_and_wire_tools(tmp_path):
    from client_registry import ClientRegistry
    from token_store import TokenStore

    database = tmp_path / "oauth.sqlite"
    acl = tmp_path / "grants.json"
    write_acl(acl, {})
    store = ScopedStore()
    app = create_app(backend=store, environment={
        "ASTER_ARCHIVE_PUBLIC_ORIGIN": ORIGIN,
        "ASTER_ARCHIVE_OAUTH_DB": str(database), "ASTER_ARCHIVE_CLIENTS": str(acl),
    })
    client_id = ClientRegistry(str(database)).register("Archive test", ["https://example.invalid/callback"])
    token = TokenStore(str(database)).issue(client_id=client_id, resource=ORIGIN + "/mcp", scope="system:admin")
    wrong_resource = TokenStore(str(database)).issue(client_id=client_id, resource="https://elsewhere.invalid/mcp", scope="system:admin")

    async def check():
        inner = app.app
        headers = {"Host": "archive-test.invalid", "X-Forwarded-Proto": "https",
                   "Accept": "application/json, text/event-stream"}
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 10000)),
                base_url=ORIGIN, headers=headers,
            ) as http:
                path = "/archive/passages/" + PID
                assert (await http.get(path)).status_code == 401
                assert (await http.get(path, headers={"Authorization": "Bearer " + wrong_resource})).status_code == 401
                authenticated = {"Authorization": "Bearer " + token}
                assert (await http.get(path, headers=authenticated)).status_code == 403
                assert store.calls == []
                write_acl(acl, {client_id: grant()})
                response = await http.get(path, headers=authenticated)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert response.json()["url"] == ORIGIN + path
                html = await http.get(path, headers={**authenticated, "Accept": "text/html"})
                assert "<script>" not in html.text and "&lt;script&gt;" in html.text
                assert "frame-ancestors 'none'" in html.headers["content-security-policy"]
                assert (await http.get("/archive/passages/" + OTHER, headers=authenticated)).status_code == 404
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "fetch", "arguments": {"id": PID}}}
                assert (await http.post("/mcp", json=payload)).status_code == 401
                wire = await http.post("/mcp", json=payload, headers=authenticated)
                assert wire.status_code == 200 and not wire.json()["result"].get("isError", False)
                write_acl(acl, {})
                assert (await http.get(path, headers=authenticated)).status_code == 403
                wire = await http.post("/mcp", json=payload, headers=authenticated)
                assert wire.json()["result"]["isError"] is True
                TokenStore(str(database)).revoke(token)
                assert (await http.get(path, headers=authenticated)).status_code == 401
    asyncio.run(asyncio.wait_for(check(), 15))


def test_backend_failures_and_oversized_passages_are_sanitized(registered, monkeypatch):
    server, store, reader, acl = registered

    def unavailable(*args, **kwargs):
        raise RuntimeError("postgresql://secret:password@private-host/private-db")
    monkeypatch.setattr(store, "fetch", unavailable)
    with caller(), pytest.raises(ToolError, match="ARCHIVE_UNAVAILABLE") as error:
        invoke(server, "fetch", {"id": PID})
    assert "password" not in str(error.value) and "private-host" not in str(error.value)

    def oversized(*args, **kwargs):
        return {"id": PID, "title": "Oversized passage", "text": "x" * (1024 * 1024), "metadata": {}}
    monkeypatch.setattr(store, "fetch", oversized)
    with caller(), pytest.raises(ToolError, match="ARCHIVE_RESPONSE_LIMIT") as error:
        invoke(server, "fetch", {"id": PID})
    assert "no text was truncated" in str(error.value)
