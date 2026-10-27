"""Panel contract and access tests through the production MCP HTTP composition.

Only the HTML asset is substituted. OAuth verification, origin checks, SDK
registration/serialization, allowlist checks, enrollment and task visibility
run unchanged against isolated temporary state. No server or paid API is used.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import time

import anyio
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.apps import APP_MIME_TYPE
from mcp.shared.inbound import NAME_BEARING_METHODS, encode_header_value
from mcp.types import CallToolResult, LATEST_PROTOCOL_VERSION
import pytest
from starlette.testclient import TestClient

from client_registry import ClientRegistry
from token_store import TokenStore


ORIGIN = "https://aster-panel.example"
HTML = "<!doctype html><html><title>Aster integration fixture</title><body>Aster</body></html>"
CLIENTS = {
    "sender": "oauth-private-sender-registration",
    "worker": "oauth-private-worker-registration",
    "other": "oauth-private-other-registration",
    "other_worker": "oauth-private-other-worker-registration",
    "unapproved": "oauth-private-unapproved-registration",
    "unenrolled": "oauth-private-unenrolled-registration",
}
PANEL_TOOLS = ("aster_panel", "aster_panel_refresh", "aster_panel_task")


@dataclass
class Harness:
    http: TestClient
    server: object
    store: object
    tokens: dict[str, str]
    token_store: TokenStore
    registry: ClientRegistry
    allowlist: Path
    clock: list[float]
    running: dict
    completed: dict
    foreign: dict
    lease_token: str

    def rpc(self, method, params=None, *, caller="sender", token=None, headers=None):
        request_headers = {"MCP-Method": method}
        name_key = NAME_BEARING_METHODS.get(method)
        if name_key is not None and name_key in (params or {}):
            request_headers["MCP-Name"] = encode_header_value(params[name_key])
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        elif caller is not None:
            request_headers["Authorization"] = "Bearer " + self.tokens[caller]
        request_headers.update(headers or {})
        request_params = {**(params or {}), "_meta": {
            "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
        }}
        return self.http.post("/mcp", headers=request_headers, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": request_params,
        })

    def call(self, name, arguments=None, **kwargs):
        response = self.rpc("tools/call", {
            "name": name, "arguments": arguments or {},
        }, **kwargs)
        assert response.status_code == 200, response.text
        envelope = response.json()
        assert "result" in envelope, envelope
        return envelope["result"]

    def tools(self):
        response = self.rpc("tools/list")
        assert response.status_code == 200, response.text
        return {tool["name"]: tool for tool in response.json()["result"]["tools"]}

    def arguments(self, name):
        return {"task_id": self.running["task_id"]} if name == "aster_panel_task" else {}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    # Keep the test independent of the frontend build while exercising the real
    # resource registration, MIME, CSP and content-versioning path.
    from coordination import panel, service

    asset_root = tmp_path / "fixture-package"
    (asset_root / "ui").mkdir(parents=True)
    (asset_root / "ui" / "aster-panel.html").write_text(HTML, encoding="utf-8")
    monkeypatch.setattr(panel, "__file__", str(asset_root / "panel.py"))

    oauth_db = tmp_path / "oauth.sqlite"
    coordination_db = tmp_path / "coordination.sqlite"
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps([
        value for name, value in CLIENTS.items() if name != "unapproved"
    ]), encoding="utf-8")
    allowlist.chmod(0o600)
    monkeypatch.setenv("ASTER_MCP_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("ASTER_MCP_OAUTH_DB", str(oauth_db))
    monkeypatch.setenv("ASTER_COORDINATION_DB", str(coordination_db))
    monkeypatch.setenv("ASTER_ADMIN_CLIENTS", str(allowlist))
    monkeypatch.setenv("ASTER_COORDINATION_NATIVE_EVENTS", "0")

    captured = {}
    clock = [time.time()]
    register = service.register_coordination_tools
    build = service.build_oauth_server

    def capture_store(*args, **kwargs):
        store = register(*args, **kwargs)
        store.clock = lambda: clock[0]
        captured["store"] = store
        return store

    def capture_server(*args, **kwargs):
        server = build(*args, **kwargs)
        captured["server"] = server
        return server

    monkeypatch.setattr(service, "register_coordination_tools", capture_store)
    monkeypatch.setattr(service, "build_oauth_server", capture_server)
    app = service.create_app()
    store = captured["store"]
    registry = ClientRegistry(str(oauth_db))
    token_store = TokenStore(str(oauth_db))
    tokens = {}
    for name, client_id in CLIENTS.items():
        registry.register_external(client_id, name, ["https://client.example/callback"])
        tokens[name] = token_store.issue(
            client_id=client_id, resource=ORIGIN + "/mcp", scope="system:admin")
        if name != "unenrolled":
            store.register_agent(
                agent_id=name, client_id=client_id, display_name=name,
                provider="integration-test",
                receive_mode="native" if name == "sender" else "pull")
    store.bind_receiver(
        agent_id="sender", subscription_id="private-subscription-sentinel",
        expected_session_id="private-session-sentinel", expires_at=clock[0] + 3600)
    store.verify_receiver(
        agent_id="sender", subscription_id="private-subscription-sentinel",
        observed_session_id="private-session-sentinel", evidence_ref="private-binding-evidence")
    store.grant(sender="sender", receiver="worker")
    store.grant(sender="other", receiver="other_worker")

    completed = store.delegate_task(
        CLIENTS["sender"], to_agent="worker", request_id="completed-request",
        instruction="Produce the public integration fixture result", expected_result="A receipt")
    completed_claim = store.claim_task(
        CLIENTS["worker"], request_id="completed-claim", task_id=completed["task_id"])
    completed = store.complete_task(
        CLIENTS["worker"], task_id=completed["task_id"],
        lease_token=completed_claim["lease_token"], fence=completed_claim["fence"],
        result={"summary": "Fixture result"}, evidence={"reference": "fixture-evidence"})
    running = store.delegate_task(
        CLIENTS["sender"], to_agent="worker", request_id="running-request",
        instruction="Visible running fixture", expected_result="A second receipt")
    claim = store.claim_task(
        CLIENTS["worker"], request_id="running-claim", task_id=running["task_id"])
    foreign = store.delegate_task(
        CLIENTS["other"], to_agent="other_worker", request_id="foreign-request",
        instruction="Unrelated task confidential sentinel", expected_result="Private receipt")

    with TestClient(
        app, base_url=ORIGIN, client=("127.0.0.1", 55000),
        headers={"X-Forwarded-Proto": "https", "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION},
    ) as http:
        yield Harness(http, captured["server"], store, tokens, token_store, registry,
                      allowlist, clock, running, completed, foreign, claim["lease_token"])


def assert_error(result, code):
    assert result.get("isError") is True, result
    assert not result.get("structuredContent"), result
    text = "\n".join(block.get("text", "") for block in result.get("content", []))
    assert code in text, result
    return text


def database_snapshot(path):
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


@contextmanager
def authenticated_context(client_id, token):
    marker = auth_context_var.set(AuthenticatedUser(AccessToken(
        token=token, client_id=client_id, scopes=["system:admin"],
        expires_at=int(time.time()) + 300, resource=ORIGIN + "/mcp")))
    try:
        yield
    finally:
        auth_context_var.reset(marker)


def test_real_sdk_registers_opening_entrypoints_and_app_only_helpers(harness):
    tools = harness.tools()
    opening = tools["aster_panel"]
    assert opening["inputSchema"]["type"] == "object"
    assert opening["inputSchema"].get("properties", {}) == {}
    assert not opening["inputSchema"].get("required")
    metadata = opening["_meta"]
    assert metadata["ui"]["visibility"] == ["model", "app"]
    assert {item["type"] for item in metadata["openai/ui"]["entrypoints"]} == {"global", "thread"}
    assert re.fullmatch(r"ui://aster/panel-[0-9a-f]+\.html", metadata["ui"]["resourceUri"])
    for name in PANEL_TOOLS:
        hints = tools[name]["annotations"]
        assert hints["readOnlyHint"] is True
        assert hints["destructiveHint"] is False
        assert hints["idempotentHint"] is True
    for name in PANEL_TOOLS[1:]:
        assert tools[name]["_meta"]["ui"]["visibility"] == ["app"]
        assert "resourceUri" not in tools[name]["_meta"]["ui"]


def test_sdk_resource_read_delivers_versioned_mime_and_closed_csp(harness):
    uri = harness.tools()["aster_panel"]["_meta"]["ui"]["resourceUri"]
    response = harness.rpc("resources/read", {"uri": uri})
    assert response.status_code == 200, response.text
    contents = response.json()["result"]["contents"]
    assert len(contents) == 1
    resource = contents[0]
    assert resource["uri"] == uri
    assert resource["mimeType"] == APP_MIME_TYPE == "text/html;profile=mcp-app"
    assert resource["text"] == HTML
    assert resource["_meta"]["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}


def test_resource_version_changes_when_served_html_changes(harness):
    from coordination import panel
    from mcp.server.mcpserver import MCPServer

    previous = harness.tools()["aster_panel"]["_meta"]["ui"]["resourceUri"]
    changed_html = HTML.replace("Aster</body>", "Updated Aster</body>")
    asset = Path(panel.__file__).with_name("ui") / "aster-panel.html"
    asset.write_text(changed_html, encoding="utf-8")
    apps, _ = panel.build_panel_app(lambda: harness.store, allowlist_path=harness.allowlist)
    replacement = MCPServer("Updated panel fixture", extensions=[apps])
    tools = anyio.run(replacement.list_tools)
    opening = next(tool for tool in tools if tool.name == "aster_panel")
    uri = opening.model_dump(by_alias=True)["_meta"]["ui"]["resourceUri"]
    assert uri != previous
    contents = anyio.run(replacement.read_resource, uri)
    assert list(contents)[0].content == changed_html


@pytest.mark.parametrize("name", PANEL_TOOLS)
def test_http_does_not_admit_unauthenticated_tool_calls(harness, name):
    response = harness.rpc("tools/call", {"name": name, "arguments": harness.arguments(name)}, caller=None)
    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    assert "Visible running fixture" not in response.text


def test_resource_http_route_is_also_bearer_protected(harness):
    uri = harness.tools()["aster_panel"]["_meta"]["ui"]["resourceUri"]
    response = harness.rpc("resources/read", {"uri": uri}, caller=None)
    assert response.status_code == 401
    assert HTML not in response.text


@pytest.mark.parametrize("credential", ["invalid", "wrong_audience", "wrong_scope", "expired", "revoked"])
def test_real_token_verifier_rejects_invalid_credentials(harness, credential):
    if credential == "invalid":
        token = "not-a-valid-access-token"
    else:
        options = {"client_id": CLIENTS["sender"], "resource": ORIGIN + "/mcp", "scope": "system:admin"}
        if credential == "wrong_audience":
            options["resource"] = "https://other-service.example/mcp"
        if credential == "wrong_scope":
            options["scope"] = "system:read"
        if credential == "expired":
            options.update(now=int(time.time()) - 100, ttl=1)
        token = harness.token_store.issue(**options)
        if credential == "revoked":
            harness.token_store.revoke(token)
    response = harness.rpc("tools/call", {"name": "aster_panel", "arguments": {}}, token=token)
    assert response.status_code == 401
    assert "structuredContent" not in response.text


@pytest.mark.parametrize("caller,code", [
    ("unapproved", "ADMIN_APPROVAL_REQUIRED"),
    ("unenrolled", "AGENT_NOT_ENROLLED"),
])
def test_every_panel_tool_requires_allowlist_and_enrollment(harness, caller, code):
    for name in PANEL_TOOLS:
        result = harness.call(name, harness.arguments(name), caller=caller)
        assert_error(result, code)
        assert "Visible running fixture" not in json.dumps(result)


def test_allowlist_is_rechecked_on_each_call_and_bad_state_fails_closed(harness):
    assert harness.call("aster_panel").get("isError") is not True
    harness.allowlist.write_text("[]", encoding="utf-8")
    for name in PANEL_TOOLS:
        assert_error(harness.call(name, harness.arguments(name)), "ADMIN_APPROVAL_REQUIRED")
    harness.allowlist.write_text('{"incorrect": "shape"}', encoding="utf-8")
    for name in PANEL_TOOLS:
        assert_error(harness.call(name, harness.arguments(name)), "AUTHORIZATION_STATE_UNAVAILABLE")
    harness.allowlist.unlink()
    assert_error(harness.call("aster_panel"), "AUTHORIZATION_STATE_UNAVAILABLE")


def test_disabled_enrollment_and_revoked_oauth_client_are_rejected(harness):
    harness.store.disable_agent("sender")
    for name in PANEL_TOOLS:
        assert_error(harness.call(name, harness.arguments(name)), "AGENT_DISABLED")
    harness.registry.revoke(CLIENTS["worker"])
    response = harness.rpc("tools/call", {"name": "aster_panel", "arguments": {}}, caller="worker")
    assert response.status_code == 401


def test_private_tasks_are_absent_and_detail_denial_does_not_confirm_existence(harness):
    snapshot = harness.call("aster_panel")
    assert snapshot.get("isError") is not True
    serialized = json.dumps(snapshot)
    assert harness.running["task_id"] in serialized
    assert harness.foreign["task_id"] not in serialized
    assert "Unrelated task confidential sentinel" not in serialized
    denied = harness.call("aster_panel_task", {"task_id": harness.foreign["task_id"]})
    absent = harness.call("aster_panel_task", {"task_id": "task_absent_integration_fixture"})
    assert assert_error(denied, "TASK_NOT_ACCESSIBLE") == assert_error(absent, "TASK_NOT_ACCESSIBLE")


def test_request_authentication_context_does_not_bleed_between_clients(harness):
    for caller in ("sender", "other", "worker", "sender"):
        result = harness.call("aster_panel_refresh", caller=caller)
        assert result.get("isError") is not True
        assert result["structuredContent"]["viewer"]["agent_id"] == caller
        if caller == "other":
            assert harness.running["task_id"] not in json.dumps(result)
        else:
            assert harness.foreign["task_id"] not in json.dumps(result)


def test_identity_cannot_be_selected_by_tool_arguments(harness):
    result = harness.call("aster_panel_refresh", {"client_id": CLIENTS["other"], "agent_id": "other"})
    # Rejecting unknown arguments and ignoring them are both safe; accepting
    # them as authority is not. The authenticated caller must remain the viewer.
    if result.get("isError") is not True:
        assert result["structuredContent"]["viewer"]["agent_id"] == "sender"
        assert harness.foreign["task_id"] not in json.dumps(result)


def test_actual_sdk_calls_return_call_tool_result_without_private_state(harness):
    async def invoke_all():
        return [await harness.server.call_tool(name, harness.arguments(name)) for name in PANEL_TOOLS]

    with authenticated_context(CLIENTS["sender"], harness.tokens["sender"]):
        results = anyio.run(invoke_all)
    for result in results:
        assert isinstance(result, CallToolResult)
        assert result.is_error is not True
        assert isinstance(result.structured_content, dict)
        assert result.content and result.content[0].type == "text"
        serialized = result.model_dump_json(by_alias=True)
        for value in (*CLIENTS.values(), *harness.tokens.values(), harness.lease_token,
                      "private-session-sentinel", "private-subscription-sentinel", "private-binding-evidence"):
            assert value not in serialized
        for field in ("_lease_token", '"lease_token"', '"client_id"',
                      '"expected_session_id"', '"observed_session_id"', '"subscription_id"'):
            assert field not in serialized


def test_open_refresh_and_detail_never_mutate_leases_receipts_or_events(harness):
    # Expired leases are especially important: ordinary Store reads may invoke
    # maintenance. A dashboard must not turn a read into task state mutation.
    harness.clock[0] += 301
    before = database_snapshot(harness.store.path)
    for name in PANEL_TOOLS:
        result = harness.call(name, harness.arguments(name))
        assert result.get("isError") is not True, result
    completed = harness.call("aster_panel_task", {"task_id": harness.completed["task_id"]})
    task = completed["structuredContent"]["task"]
    assert task["status"] == "completed"
    assert task["result_verification"] == "worker_reported"
    assert task["acceptance"] == "pending"
    assert database_snapshot(harness.store.path) == before


def test_http_receipt_matches_existing_outcome_feed_and_is_sender_only(harness):
    arguments = {"task_id": harness.completed["task_id"]}
    sender_task = harness.call("aster_panel_task", arguments)["structuredContent"]["task"]
    receipt = sender_task["receipt"]
    feed_result = harness.call("aster_outcomes")
    assert feed_result.get("isError") is not True
    # Existing Coordinator dict tools use the SDK's JSON text result. The new
    # panel deliberately adds structuredContent for the host/UI bridge.
    feed = feed_result.get("structuredContent") or json.loads(feed_result["content"][0]["text"])
    outcomes = feed["outcomes"]
    saved = next(item for item in outcomes if item["task_id"] == arguments["task_id"])
    assert receipt["kind"] == "coordinator_outcome_record"
    for field in ("outcome_id", "sequence", "task_revision", "status", "result_verification", "acceptance"):
        assert receipt[field] == saved[field]
    assert receipt["result_verification"] == "worker_reported"
    assert receipt["acceptance"] == "pending"
    receiver_task = harness.call("aster_panel_task", arguments, caller="worker")["structuredContent"]["task"]
    assert "receipt" not in receiver_task
    running = harness.call("aster_panel_task", {"task_id": harness.running["task_id"]})
    assert "receipt" not in running["structuredContent"]["task"]


@pytest.mark.parametrize("bad_limit", [True, "10", 0, 101])
def test_refresh_bounds_cannot_be_bypassed_through_sdk_coercion(harness, bad_limit):
    result = harness.call("aster_panel_refresh", {"limit": bad_limit})
    assert result.get("isError") is True
    assert not result.get("structuredContent")


def test_existing_public_origin_gate_still_wraps_panel_http(harness):
    for headers in ({"X-Forwarded-Proto": "http"}, {"Host": "attacker.example"}):
        response = harness.rpc("tools/call", {"name": "aster_panel", "arguments": {}}, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"] == "untrusted_proxy_origin"


def publish_sources(harness, sources, *, audience=None):
    path = harness.store.path.with_name("panel-sources.json")
    path.write_text(json.dumps({
        "allowed_agent_ids": ["sender"] if audience is None else audience,
        "sources": sources,
        # Ancillary state must never replace the authenticated viewer/tasks.
        "viewer": {"agent_id": "other"},
        "tasks": [harness.foreign],
    }), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_ancillary_metrics_preserve_valid_fields_but_cannot_inject_private_state(harness):
    publish_sources(harness, {
        "archive": {
            "status": "snapshot", "conversations": 12,
            "checked_at": "2026-10-27T08:00:00Z",
            "complete_account_history_verified": False,
            "client_id": CLIENTS["other"], "lease_token": harness.lease_token,
            "tasks": [harness.foreign], "internal_path": "/private/ancillary-path-sentinel",
        },
        "costs": {
            "status": "available", "currency": "USD", "date": "2026-10-27",
            "daily_budget_usd": 10.5, "reported_spend_usd": 2.25,
            "paid_dispatch_enabled": False, "access_token": harness.tokens["other"],
        },
    })
    result = harness.call("aster_panel")
    assert result.get("isError") is not True
    data = result["structuredContent"]
    assert data["viewer"]["agent_id"] == "sender"
    assert data["sources"]["archive"]["conversations"] == 12
    assert data["sources"]["archive"]["complete_account_history_verified"] is False
    assert data["sources"]["costs"]["daily_budget_usd"] == 10.5
    assert data["sources"]["costs"]["paid_dispatch_enabled"] is False
    serialized = json.dumps(result)
    for private in (CLIENTS["other"], harness.lease_token, harness.tokens["other"],
                    harness.foreign["task_id"], "/private/ancillary-path-sentinel"):
        assert private not in serialized


def test_ancillary_source_audience_cannot_expand_task_permissions(harness):
    before = harness.call("aster_panel")["structuredContent"]["sources"]
    publish_sources(harness, {
        "archive": {"status": "snapshot", "conversations": 98765},
        "costs": {"status": "available", "reported_spend_usd": 123.45},
    }, audience=["other"])
    data = harness.call("aster_panel")["structuredContent"]
    for name in ("archive", "costs"):
        assert data["sources"][name] == before[name]
    # Being an allowed telemetry viewer still cannot expose another task.
    data = harness.call("aster_panel", caller="other")["structuredContent"]
    assert data["sources"]["archive"]["conversations"] == 98765
    assert harness.running["task_id"] not in json.dumps(data)


def test_ancillary_metrics_reject_nonfinite_negative_and_coerced_numbers(harness):
    publish_sources(harness, {
        "archive": {"status": "snapshot", "conversations": True,
                    "exact_passages": -1, "revisions": "200", "completed_imports": 3},
        "costs": {"status": "available", "daily_budget_usd": float("inf"),
                  "reported_spend_usd": -2.0, "reserved_spend_usd": True,
                  "remaining_budget_usd": 4.0},
    })
    data = harness.call("aster_panel")["structuredContent"]
    assert data["sources"]["archive"]["completed_imports"] == 3
    assert data["sources"]["costs"]["remaining_budget_usd"] == 4.0
    for key in ("conversations", "exact_passages", "revisions"):
        assert key not in data["sources"]["archive"]
    for key in ("daily_budget_usd", "reported_spend_usd", "reserved_spend_usd"):
        assert key not in data["sources"]["costs"]


@pytest.mark.parametrize("problem", ["symlink", "oversized", "malformed", "public_permissions"])
def test_untrusted_ancillary_file_never_blocks_or_replaces_core_data(harness, problem):
    baseline = harness.call("aster_panel")["structuredContent"]["sources"]
    path = publish_sources(harness, {"archive": {"status": "snapshot", "conversations": 98765}})
    if problem == "symlink":
        target = path.with_name("auxiliary-target.json")
        path.rename(target)
        path.symlink_to(target)
    elif problem == "oversized":
        path.write_text(" " * 70000 + path.read_text(), encoding="utf-8")
    elif problem == "malformed":
        path.write_text("{ ancillary-content-must-not-be-echoed", encoding="utf-8")
    else:
        path.chmod(0o644)
    result = harness.call("aster_panel")
    assert result.get("isError") is not True
    data = result["structuredContent"]
    assert data["viewer"]["agent_id"] == "sender"
    assert harness.running["task_id"] in json.dumps(data)
    assert data["sources"]["archive"] == baseline["archive"]
    assert "ancillary-content-must-not-be-echoed" not in json.dumps(result)
