"""Coordination's real SDK tool and OAuth boundaries, without native execution.

Storage is recorded here so failures identify the gateway boundary rather than
retesting the task state machine. Both platform registrations use their real
OAuth composition; Windows process/transfer modules are isolated stubs.
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
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


CALLS = {
    "aster_profile": {},
    "aster_list_agents": {},
    "aster_delegate_task": {"to_agent": "receiver", "request_id": "send-1",
                            "instruction": "Count the supplied items", "expected_result": "A count"},
    "aster_inbox": {},
    "aster_claim_task": {"request_id": "claim-1"},
    "aster_renew_lease": {"task_id": "task_test", "lease_token": "private-lease", "fence": 1},
    "aster_checkpoint_task": {"task_id": "task_test", "lease_token": "private-lease", "fence": 1,
                              "expected_revision": 1, "checkpoint": {"progress": "ready"}},
    "aster_complete_task": {"task_id": "task_test", "lease_token": "private-lease", "fence": 1,
                            "result": {"count": 3}},
    "aster_task_status": {"task_id": "task_test"},
    "aster_cancel_task": {"task_id": "task_test", "reason": "No longer needed"},
    "aster_reconcile_task": {"task_id": "task_test", "decision": "cancel",
                             "evidence": {"reviewed": True}, "expected_revision": 1},
    "aster_verify_result": {"task_id": "task_test", "expected_revision": 1, "accepted": True,
                            "evidence": {"checks": ["expected count"], "reference": "test-result"}},
}


@contextmanager
def caller(client_id="approved-client", *, scopes=None, token="private-access-token"):
    credential = AccessToken(token=token, client_id=client_id,
                             scopes=["system:admin"] if scopes is None else scopes)
    marker = auth_context_var.set(AuthenticatedUser(credential))
    try:
        yield
    finally:
        auth_context_var.reset(marker)


def invoke(server, name, arguments=None):
    result = asyncio.run(server.call_tool(name, arguments or {}))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(part.text for part in result.content if part.type == "text"))


class RecordingStore:
    def __init__(self, path):
        self.path = path
        self.calls = []
        self.failure = None

    def __getattr__(self, name):
        if "aster_" + name not in CALLS:
            raise AttributeError(name)

        def call(client_id, **arguments):
            self.calls.append((name, client_id, arguments))
            if self.failure is not None:
                raise self.failure
            return {"method": name, "authenticated_client": client_id}
        return call


@pytest.fixture(params=["linux", "windows"])
def gateway_factory(request, tmp_path, monkeypatch):
    import coordination.tools as coordination_tools
    import oauth_runtime
    import oauth_service

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('["approved-client"]')
    database = tmp_path / "oauth.sqlite"
    coord_database = tmp_path / "coordination.sqlite"
    origin = "https://coordination-test.invalid"
    captured, stores, native_installs = {}, [], []
    real_build = oauth_runtime.build_oauth_server

    def build(**kwargs):
        server = real_build(**kwargs)
        captured["server"] = server
        return server

    def new_store(path):
        instance = RecordingStore(path)
        stores.append(instance)
        return instance

    def forbidden_backend(*args, **kwargs):
        raise AssertionError("coordination must not call a native host backend")

    # Native dispatch has its own integration suite. This fixture verifies only
    # the explicit opt-in and composition, without starting a delivery worker.
    native_module = ModuleType("coordination.native")
    native_module.NativeEvents = lambda store, **kwargs: (store, kwargs)
    native_module.install_native_events = lambda server, service: native_installs.append((server, service))
    monkeypatch.setitem(sys.modules, "coordination.native", native_module)
    monkeypatch.setattr(coordination_tools, "Store", new_store)
    for key, value in {
        "ASTER_MCP_PUBLIC_ORIGIN": origin,
        "ASTER_MCP_OAUTH_DB": str(database),
        "ASTER_ALLOWED_ROOT": str(workspace),
        "ASTER_WORKSPACE": str(workspace),
        "ASTER_ADMIN_CLIENTS": str(allowlist),
    }.items():
        monkeypatch.setenv(key, value)

    if request.param == "linux":
        module = oauth_service
        monkeypatch.setattr(module, "build_oauth_server", build)
        for name in ("run_admin", "start_admin_job", "get_admin_job", "cancel_admin_job",
                     "admin_begin_upload", "admin_upload_chunk", "admin_finish_upload",
                     "admin_download_info", "admin_download_chunk"):
            monkeypatch.setattr(module, name, forbidden_backend)
    else:
        monkeypatch.setenv("ASTER_WINDOWS_PUBLIC_ORIGIN", origin)
        monkeypatch.setenv("ASTER_WINDOWS_OAUTH_DB", str(database))
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
                setattr(stub, function, forbidden_backend)
            monkeypatch.setitem(sys.modules, name, stub)
        monkeypatch.setattr(sys, "path", sys.path[:])
        spec = importlib.util.spec_from_file_location(
            "coordination_test_windows_gateway",
            Path(__file__).resolve().parents[1] / "platforms/windows/oauth_gateway.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    def create(*, enabled=True, native=None, path=None):
        if enabled:
            monkeypatch.setenv("ASTER_COORDINATION_DB", str(coord_database) if path is None else path)
        else:
            monkeypatch.delenv("ASTER_COORDINATION_DB", raising=False)
        if native is None:
            monkeypatch.delenv("ASTER_COORDINATION_NATIVE_EVENTS", raising=False)
        else:
            monkeypatch.setenv("ASTER_COORDINATION_NATIVE_EVENTS", native)
        app = module.create_app()
        return SimpleNamespace(app=app, server=captured["server"], stores=stores,
                               store=stores[-1] if stores else None, allowlist=allowlist,
                               database=database, origin=origin, native_installs=native_installs)

    return create


def tool_names(server):
    return {tool.name for tool in asyncio.run(server.list_tools())}


def test_optional_registration_preserves_existing_surface_and_hides_operator_actions(gateway_factory):
    disabled = gateway_factory(enabled=False)
    baseline = tool_names(disabled.server)
    assert not baseline.intersection(CALLS)
    assert disabled.stores == [] and disabled.native_installs == []
    enabled = gateway_factory()
    assert tool_names(enabled.server) == baseline | set(CALLS)
    assert len(enabled.stores) == 1 and enabled.native_installs == []
    for tool in asyncio.run(enabled.server.list_tools()):
        if tool.name in CALLS:
            properties = tool.input_schema.get("properties", {})
            assert not {"client_id", "sender", "from_agent", "principal", "agent_id"}.intersection(properties)
    # Exact surface equality also excludes enrollment, grants and native binding
    # verification functions from remote MCP calls.


def test_every_tool_authenticates_scope_and_current_grant_before_storage(gateway_factory):
    g = gateway_factory()
    for name, arguments in CALLS.items():
        with pytest.raises(ToolError, match="AUTHENTICATION_REQUIRED"):
            invoke(g.server, name, arguments)
        with caller(scopes=[]), pytest.raises(ToolError, match="OAUTH_SCOPE_REQUIRED"):
            invoke(g.server, name, arguments)
        with caller("unapproved-client"), pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED"):
            invoke(g.server, name, arguments)
    assert g.store.calls == []


def test_context_identity_is_used_for_every_tool_and_revocation_is_immediate(gateway_factory):
    g = gateway_factory()
    with caller():
        for name, arguments in CALLS.items():
            result = invoke(g.server, name, arguments)
            assert result["authenticated_client"] == "approved-client"
        assert len(g.store.calls) == len(CALLS)
        assert all(call[1] == "approved-client" for call in g.store.calls)
        g.allowlist.write_text("[]")
        for name, arguments in CALLS.items():
            with pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED"):
                invoke(g.server, name, arguments)
    with caller(token="renewed-private-token"), pytest.raises(ToolError, match="ADMIN_APPROVAL_REQUIRED"):
        invoke(g.server, "aster_profile")
    assert len(g.store.calls) == len(CALLS)


def test_bad_allowlist_is_configuration_failure_not_permission_or_store_access(gateway_factory):
    g = gateway_factory()
    g.allowlist.write_text('{"private-id":"not-an-allowlist"}')
    with caller(), pytest.raises(ToolError, match="AUTHORIZATION_STATE_UNAVAILABLE") as denied:
        invoke(g.server, "aster_profile")
    assert "private-id" not in str(denied.value)
    assert g.store.calls == []


def test_caller_supplied_principal_cannot_override_authenticated_identity(gateway_factory):
    g = gateway_factory()
    with caller():
        try:
            result = invoke(g.server, "aster_profile", {"client_id": "victim", "agent_id": "victim"})
        except ToolError:
            # A strict SDK input model may reject extra fields outright.
            assert g.store.calls == []
        else:
            # If unknown fields are ignored, identity still comes only from the
            # authenticated context, never from the unadvertised arguments.
            assert result["authenticated_client"] == "approved-client"
            assert g.store.calls == [("profile", "approved-client", {})]


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_fence_is_not_coerced_from_boolean_string_or_float(gateway_factory, value):
    g = gateway_factory()
    arguments = dict(CALLS["aster_renew_lease"], fence=value)
    with caller(), pytest.raises(ToolError):
        invoke(g.server, "aster_renew_lease", arguments)
    assert g.store.calls == []


def test_expected_store_errors_are_public_but_runtime_details_are_not(gateway_factory):
    from coordination.store import CoordinationError
    g = gateway_factory()
    g.store.failure = CoordinationError("TASK_NOT_FOUND", "Task is not available to this agent")
    with caller(), pytest.raises(ToolError, match="TASK_NOT_FOUND") as expected:
        invoke(g.server, "aster_task_status", CALLS["aster_task_status"])
    assert not isinstance(expected.value, UnexpectedToolError)
    g.store.failure = OSError("private-database-location-and-content")
    with caller(), pytest.raises(UnexpectedToolError) as failed:
        invoke(g.server, "aster_task_status", CALLS["aster_task_status"])
    assert "private-database" not in str(failed.value)


@pytest.mark.parametrize("native", [None, "0", "1"])
def test_native_event_adapter_requires_explicit_opt_in(gateway_factory, native):
    g = gateway_factory(native=native)
    assert len(g.native_installs) == (1 if native == "1" else 0)
    if native == "1":
        server, (store, options) = g.native_installs[0]
        assert server is g.server and store is g.store
        assert Path(options["allowlist_path"]) == g.allowlist
        assert Path(options["oauth_db_path"]) == g.database


@pytest.mark.parametrize("native", ["", "true", "yes", "2"])
def test_invalid_native_flag_fails_configuration(gateway_factory, native):
    with pytest.raises(ValueError, match="ASTER_COORDINATION_NATIVE_EVENTS"):
        gateway_factory(native=native)


def test_native_events_without_coordination_database_fail_configuration(gateway_factory):
    with pytest.raises(ValueError, match="require ASTER_COORDINATION_DB"):
        gateway_factory(enabled=False, native="1")
    disabled = gateway_factory(enabled=False, native="0")
    assert disabled.stores == [] and disabled.native_installs == []


@pytest.mark.parametrize("path", ["", "relative.sqlite"])
def test_enabled_coordination_requires_absolute_database_path(gateway_factory, path):
    with pytest.raises(ValueError, match="absolute database path"):
        gateway_factory(path=path)


@pytest.mark.parametrize("alias", [False, True])
def test_coordination_cannot_reuse_oauth_database_before_any_database_write(gateway_factory, tmp_path, alias):
    database = tmp_path / "oauth.sqlite"
    path = tmp_path / "unused" / ".." / "oauth.sqlite" if alias else database
    with pytest.raises(ValueError, match="databases must be separate"):
        gateway_factory(path=str(path))
    assert not database.exists()


def test_real_oauth_wire_identity_and_allowlist_revocation(gateway_factory):
    """Exercise each platform's ASGI bearer middleware, not a mocked principal."""
    import httpx
    from client_registry import ClientRegistry
    from token_store import TokenStore

    g = gateway_factory()
    client_id = ClientRegistry(str(g.database)).register(
        "Coordination boundary test", ["https://callback.invalid/oauth"])
    tokens = TokenStore(str(g.database))
    access = tokens.issue(client_id=client_id, resource=g.origin + "/mcp", scope="system:admin")
    wrong_resource = tokens.issue(client_id=client_id, resource="https://elsewhere.invalid/mcp",
                                  scope="system:admin")

    async def check():
        inner = g.app
        while not hasattr(inner, "router"):
            inner = inner.app
        headers = {"Host": "coordination-test.invalid", "X-Forwarded-Proto": "https",
                   "Accept": "application/json, text/event-stream"}
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=g.app, client=("127.0.0.1", 1234)),
                base_url=g.origin, headers=headers,
            ) as http:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "aster_profile", "arguments": {}}}
                assert (await http.post("/mcp", json=payload)).status_code == 401
                assert (await http.post("/mcp", json=payload,
                        headers={"Authorization": "Bearer " + wrong_resource})).status_code == 401
                authenticated = {"Authorization": "Bearer " + access}
                denied = await http.post("/mcp", json=payload, headers=authenticated)
                assert denied.status_code == 200 and denied.json()["result"]["isError"]
                assert "ADMIN_APPROVAL_REQUIRED" in denied.text and access not in denied.text
                assert g.store.calls == []

                g.allowlist.write_text(json.dumps([client_id]))
                approved = await http.post("/mcp", json=payload, headers=authenticated)
                assert approved.status_code == 200 and not approved.json()["result"].get("isError", False)
                assert g.store.calls == [("profile", client_id, {})]
                g.allowlist.write_text("[]")
                revoked = await http.post("/mcp", json=payload, headers=authenticated)
                assert revoked.status_code == 200 and revoked.json()["result"]["isError"]
                assert "ADMIN_APPROVAL_REQUIRED" in revoked.text and access not in revoked.text
                assert g.store.calls == [("profile", client_id, {})]
    asyncio.run(asyncio.wait_for(check(), timeout=15))
