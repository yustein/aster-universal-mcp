"""Per-client onboarding and deliberate SDK errors, without native execution.

Windows gateway registration uses isolated backend stubs on every platform.
These tests exercise its real tool definitions/SDK wrapping, not Win32 behavior.
"""
import asyncio
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tool_access


@contextmanager
def caller(client_id="this-client", *, scopes=None, token="private-access-token"):
    access = AccessToken(token=token, client_id=client_id,
                         scopes=["system:admin"] if scopes is None else scopes)
    marker = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(marker)


def invoke(server, name, arguments=None):
    result = asyncio.run(server.call_tool(name, arguments or {}))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(part.text for part in result.content if part.type == "text"))


@pytest.mark.parametrize("raw", [
    b'{"this-client":true}', b'"this-client"', b'null', b'[true]',
    b'[1]', b'[null]', b'[""]', b'["bad\\u0000id"]', b'{invalid', b'\xff',
    b'[' * 2000 + b']' * 2000,
])
def test_invalid_authorization_state_fails_closed_without_reflecting_contents(tmp_path, raw):
    path = tmp_path / "allowlist.json"
    path.write_bytes(raw)
    with caller(), pytest.raises(ToolError, match="AUTHORIZATION_STATE_UNAVAILABLE") as denied:
        tool_access.current_client_access(path)
    assert "ADMIN_APPROVAL_REQUIRED" not in str(denied.value)
    assert str(path) not in str(denied.value)


def test_missing_unreadable_and_oversized_authorization_state_are_configuration_errors(tmp_path, monkeypatch):
    path = tmp_path / "allowlist.json"
    with caller(), pytest.raises(ToolError, match="AUTHORIZATION_STATE_UNAVAILABLE"):
        tool_access.current_client_access(path)
    path.write_bytes(b" " * (tool_access.MAX_ALLOWLIST_BYTES + 1))
    with pytest.raises(ToolError, match="AUTHORIZATION_STATE_UNAVAILABLE"):
        tool_access.read_admin_allowlist(path)
    def unreadable(*args, **kwargs):
        raise PermissionError("private-config-error-value")
    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(ToolError, match="AUTHORIZATION_STATE_UNAVAILABLE") as error:
        tool_access.read_admin_allowlist(path)
    assert "private-config-error-value" not in str(error.value)


def test_authentication_and_scope_are_checked_before_missing_grant_state(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(ToolError, match="AUTHENTICATION_REQUIRED"):
        tool_access.current_client_access(missing)
    with caller(scopes=[]), pytest.raises(ToolError, match="OAUTH_SCOPE_REQUIRED"):
        tool_access.current_client_access(missing)


def test_allowlist_bom_exact_membership_and_same_token_revocation(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text('["THIS-client", "other-client"]', encoding="utf-8-sig")
    with caller():
        assert not tool_access.current_client_access(path).admin_approved
        path.write_text('["this-client"]', encoding="utf-8-sig")
        assert tool_access.current_client_access(path).admin_approved
        path.write_text("[]", encoding="utf-8-sig")
        assert not tool_access.current_client_access(path).admin_approved


@pytest.fixture(params=["linux", "windows"])
def registered_gateway(request, tmp_path, monkeypatch):
    """Real platform tool registration; isolated backend calls cannot execute."""
    import oauth_runtime
    import oauth_service

    workspace, listing_root = tmp_path / "workspace", tmp_path / "listing-root"
    workspace.mkdir()
    listing_root.mkdir()
    (workspace / "workspace-entry").write_text("workspace")
    (listing_root / "root-entry").write_text("root")
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('["unrelated-client-private-id"]')
    calls, captured, failures = [], {}, {}

    def backend(name):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            if name in failures:
                raise failures[name]
            return {"status": "completed", "exit_code": 0}
        return call

    def build(**kwargs):
        server = MCPServer("isolated platform tool registration")
        kwargs["register_tools"](server)
        captured["server"] = server
        return server

    monkeypatch.setenv("ASTER_MCP_PUBLIC_ORIGIN", "https://tool-test.invalid")
    monkeypatch.setenv("ASTER_MCP_OAUTH_DB", str(tmp_path / "linux.sqlite"))
    monkeypatch.setenv("ASTER_ALLOWED_ROOT", str(listing_root))
    monkeypatch.setenv("ASTER_WORKSPACE", str(workspace))
    monkeypatch.setenv("ASTER_ADMIN_CLIENTS", str(allowlist))
    if request.param == "linux":
        module = oauth_service
        monkeypatch.setattr(module, "build_oauth_server", build)
        for name in ("run_admin", "start_admin_job", "get_admin_job", "cancel_admin_job",
                     "admin_begin_upload", "admin_upload_chunk", "admin_finish_upload",
                     "admin_download_info", "admin_download_chunk"):
            monkeypatch.setattr(module, name, backend(name))
    else:
        monkeypatch.setenv("ASTER_WINDOWS_PUBLIC_ORIGIN", "https://tool-test.invalid")
        monkeypatch.setenv("ASTER_WINDOWS_OAUTH_DB", str(tmp_path / "windows.sqlite"))
        monkeypatch.setenv("ASTER_WINDOWS_ADMIN_CLIENTS", str(allowlist))
        monkeypatch.setenv("ASTER_WINDOWS_WORKSPACE", str(workspace))
        monkeypatch.setattr(oauth_runtime, "build_oauth_server", build)
        for name, functions in {
            "executor": ["run_command"], "job_launcher": ["start_job"],
            "jobs": ["job_status"], "job_cancel": ["cancel_job"],
            "transfer": ["begin_upload", "upload_chunk"],
            "upload_finish": ["finish_upload"], "download": ["download_info", "download_chunk"],
        }.items():
            stub = ModuleType(name)
            for function in functions:
                setattr(stub, function, backend(function))
            monkeypatch.setitem(sys.modules, name, stub)
        # The entrypoint adds its source root to sys.path; isolate that change.
        monkeypatch.setattr(sys, "path", sys.path[:])
        spec = importlib.util.spec_from_file_location(
            "isolated_windows_gateway", Path(__file__).resolve().parents[1] / "platforms/windows/oauth_gateway.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module.create_app()
    return SimpleNamespace(platform=request.param, server=captured["server"], calls=calls,
                           allowlist=allowlist, workspace=workspace, listing_root=listing_root,
                           module=module, failures=failures)


def test_connection_status_exposes_only_this_client_and_never_grants(registered_gateway):
    g = registered_gateway
    original = g.allowlist.read_bytes()
    with caller():
        result = invoke(g.server, "connection_status")
    assert result["status"] == "approval_required"
    assert result["client_id"] == "this-client" and result["authenticated"] is True
    assert result["platform"] == g.platform and not result["admin_approved"]
    assert not result["permissions"]["commands"]
    assert result["permissions"]["directory_listing"] is (g.platform == "linux")
    assert result["default_workspace"] == str(g.workspace)
    assert result["listing_base"] == str(g.listing_root if g.platform == "linux" else g.workspace)
    assert "no new pairing" in result["next_step"]
    if g.platform == "windows":
        assert "-Action clients" in result["next_step"]
    serialized = json.dumps(result)
    assert "unrelated-client-private-id" not in serialized and "private-access-token" not in serialized
    assert g.allowlist.read_bytes() == original and g.calls == []
    with pytest.raises(ToolError, match="AUTHENTICATION_REQUIRED"):
        invoke(g.server, "connection_status")


def test_every_privileged_tool_denies_before_backend_with_actionable_sdk_error(registered_gateway):
    g = registered_gateway
    linux = {
        "run_command": {"command": ["hostname"]}, "start_job": {"command": ["hostname"]},
        "job_status": {"job_id": "test-job"}, "cancel_job": {"job_id": "test-job"},
        "begin_upload": {"destination": "/test", "size": 1, "sha256": "0" * 64},
        "upload_chunk": {"transfer_id": "test-transfer", "offset": 0, "data_base64": "YQ=="},
        "finish_upload": {"transfer_id": "test-transfer"},
        "download_info": {"path": "/test"}, "download_chunk": {"path": "/test", "offset": 0},
    }
    names = dict(zip(linux, ["execute_command", "start_background_job", "background_job_status",
                            "cancel_background_job", "begin_file_upload", "append_file_chunk",
                            "complete_file_upload", "inspect_file", "read_file_chunk"]))
    with caller():
        for name, arguments in linux.items():
            with pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED") as denied:
                invoke(g.server, name if g.platform == "linux" else names[name], arguments)
            assert not isinstance(denied.value, UnexpectedToolError)
            assert '"this-client"' in str(denied.value)
            assert "unrelated-client-private-id" not in str(denied.value)
    assert g.calls == []


def test_status_and_execution_recheck_grant_with_existing_and_new_token(registered_gateway):
    g = registered_gateway
    name = "run_command" if g.platform == "linux" else "execute_command"
    with caller():
        g.allowlist.write_text('["this-client"]')
        status = invoke(g.server, "connection_status")
        assert status["status"] == "approved" and status["permissions"]["commands"]
        assert "does not verify executor health" in status["next_step"]
        assert "hostname" in status["next_step"] and "exit_code 0" in status["next_step"]
        invoke(g.server, name, {"command": ["hostname"]})
        assert g.calls[-1][1][1] == str(g.workspace)
        g.allowlist.write_text("[]")
        assert invoke(g.server, "connection_status")["status"] == "approval_required"
        with pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED"):
            invoke(g.server, name, {"command": ["hostname"]})
    with caller(token="renewed-private-token"), pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED"):
        invoke(g.server, name, {"command": ["hostname"]})
    assert len(g.calls) == 1


def test_listing_policy_changes_immediately_when_grant_is_removed(registered_gateway, tmp_path):
    g = registered_gateway
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside-entry").write_text("outside")
    with caller():
        if g.platform == "linux":
            listing = invoke(g.server, "list_directory")
            assert listing["path"] == str(g.listing_root)
            assert listing["entries"][0]["name"] == "root-entry"
        else:
            with pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED"):
                invoke(g.server, "list_directory")
        with pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED"):
            invoke(g.server, "list_directory", {"path": str(outside)})
        g.allowlist.write_text('["this-client"]')
        listing = invoke(g.server, "list_directory", {"path": str(outside)})
        assert listing["path"] == str(outside)
        if g.platform == "windows":
            assert listing["entries"] == ["outside-entry"]
        else:
            assert listing["entries"][0]["name"] == "outside-entry"
        if g.platform == "linux":
            with pytest.raises(ToolError, match="LISTING_PATH_OUTSIDE_ROOT"):
                invoke(g.server, "list_directory", {"path": "../outside"})
        g.allowlist.write_text("[]")
        with pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED"):
            invoke(g.server, "list_directory", {"path": str(outside)})
        if g.platform == "linux":
            assert invoke(g.server, "list_directory")["path"] == str(g.listing_root)


def test_runtime_failure_is_not_reclassified_as_missing_approval(registered_gateway):
    g = registered_gateway
    g.failures["run_admin" if g.platform == "linux" else "run_command"] = PermissionError("private-backend-detail")
    g.allowlist.write_text('["this-client"]')
    with caller(), pytest.raises(UnexpectedToolError) as failed:
        invoke(g.server, "run_command" if g.platform == "linux" else "execute_command", {"command": ["hostname"]})
    assert "ADMIN_APPROVAL_REQUIRED" not in str(failed.value)
    assert "private-backend-detail" not in str(failed.value)


def test_http_connection_status_and_approval_denial_are_authenticated_and_actionable(tmp_path, monkeypatch):
    """Real ASGI OAuth middleware and MCP wire results; no native process launched."""
    import httpx
    from client_registry import ClientRegistry
    from token_store import TokenStore
    from oauth_service import create_app

    root = tmp_path / "root"
    root.mkdir()
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]")
    database = tmp_path / "oauth.sqlite"
    origin = "https://tool-test.invalid"
    for key, value in {"ASTER_MCP_PUBLIC_ORIGIN": origin, "ASTER_MCP_OAUTH_DB": str(database),
                       "ASTER_ALLOWED_ROOT": str(root), "ASTER_WORKSPACE": str(root),
                       "ASTER_ADMIN_CLIENTS": str(allowlist)}.items():
        monkeypatch.setenv(key, value)
    app = create_app()
    cid = ClientRegistry(str(database)).register("This test client", ["https://example.invalid/callback"])
    token = TokenStore(str(database)).issue(client_id=cid, resource=origin + "/mcp", scope="system:admin")

    async def check():
        inner = app
        while not hasattr(inner, "router"):
            inner = inner.app
        headers = {"Host": "tool-test.invalid", "X-Forwarded-Proto": "https",
                   "Accept": "application/json, text/event-stream"}
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                         base_url=origin, headers=headers) as http:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "connection_status", "arguments": {}}}
                assert (await http.post("/mcp", json=payload)).status_code == 401
                authenticated = {"Authorization": "Bearer " + token}
                status = await http.post("/mcp", json=payload, headers=authenticated)
                assert status.status_code == 200
                result = status.json()["result"]
                assert not result.get("isError", False)
                body = result.get("structuredContent")
                if body is None:
                    body = json.loads(next(part["text"] for part in result["content"] if part["type"] == "text"))
                assert body["client_id"] == cid
                assert body["status"] == "approval_required"
                payload["params"] = {"name": "run_command", "arguments": {"command": ["must-not-execute"]}}
                denied = await http.post("/mcp", json=payload, headers=authenticated)
                assert denied.status_code == 200 and denied.json()["result"]["isError"]
                text = denied.json()["result"]["content"][0]["text"]
                assert "ADMIN_APPROVAL_REQUIRED" in text and cid in text
                assert token not in text
                allowlist.write_text('{"this-is-not-an-allowlist":true}')
                unavailable = await http.post("/mcp", json=payload, headers=authenticated)
                text = unavailable.json()["result"]["content"][0]["text"]
                assert "AUTHORIZATION_STATE_UNAVAILABLE" in text
                assert "ADMIN_APPROVAL_REQUIRED" not in text and "this-is-not-an-allowlist" not in text
    asyncio.run(asyncio.wait_for(check(), timeout=15))
