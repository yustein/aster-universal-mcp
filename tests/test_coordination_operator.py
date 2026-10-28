"""Real enrollment checks and companion composition, with no production state."""
import asyncio
import json
from pathlib import Path
import sqlite3

import pytest

from client_registry import ClientRegistry
from coordination.cli import parser, run
from coordination.store import Store
from coordination import service
from test_coordination_tools import caller, invoke


def args(db, *values):
    return parser().parse_args(["--db", str(db), *map(str, values)])


@pytest.fixture
def operator(tmp_path):
    oauth = tmp_path / "oauth.sqlite"
    registry = ClientRegistry(str(oauth))
    client = registry.register("Operator test", ["https://example.invalid/callback"])
    with sqlite3.connect(oauth) as db:
        db.execute("CREATE TABLE client_metadata(client_id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO client_metadata VALUES (?)", (client,))
    allowlist = tmp_path / "allowed.json"
    allowlist.write_text(json.dumps([client]))
    coordination = tmp_path / "coord.sqlite"
    enroll = ["enroll", "--oauth-db", oauth, "--allowlist", allowlist,
              "--agent-id", "researcher", "--client-id", client, "--name", "Researcher", "--provider", "anthropic"]
    return coordination, oauth, allowlist, client, enroll


def test_enrollment_reuses_actual_active_approved_client_without_creating_grants(operator):
    coord, oauth, allowed, client, enroll = operator
    before = allowed.read_bytes()
    result = run(args(coord, *enroll))
    assert result["agent"]["agent_id"] == "researcher"
    assert result["checked_registration"]["client_id"] == client
    assert allowed.read_bytes() == before
    with sqlite3.connect(oauth) as db:
        assert db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] == 1
    assert run(args(coord, "agents"))["agents"][0]["client_id"] == client


def test_nonapproved_or_revoked_client_cannot_enroll(operator):
    coord, oauth, allowed, client, enroll = operator
    allowed.write_text("[]")
    with pytest.raises(ValueError, match="approval"):
        run(args(coord, *enroll))
    assert not coord.exists()
    allowed.write_text(json.dumps([client]))
    ClientRegistry(str(oauth)).revoke(client)
    with pytest.raises(ValueError, match="registered and active"):
        run(args(coord, *enroll))
    assert not coord.exists()


def test_enrollment_refuses_oauth_database_reuse_before_writing(operator):
    _, oauth, _, _, enroll = operator
    with pytest.raises(ValueError, match="separate"):
        run(args(oauth, *enroll))
    with sqlite3.connect(oauth) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'coord_%'").fetchone()[0] == 0


def test_local_operator_routes_binding_and_disable(tmp_path):
    db = tmp_path / "coord.sqlite"
    assert run(args(db, "init"))["grants_created"] is False
    store = Store(db)
    store.register_agent(agent_id="a", client_id="a-client", display_name="A", provider="openai")
    store.register_agent(agent_id="b", client_id="b-client", display_name="B", provider="openai", receive_mode="native")
    assert run(args(db, "grant", "a", "b"))["allowed"] is True
    binding = store.bind_receiver(agent_id="b", subscription_id="sub-1", expected_session_id="actual-chat",
                                  expires_at=store.clock() + 3600)
    assert run(args(db, "binding", "b"))["binding"]["ready"] is False
    observed = run(args(db, "verify-receiver", "b", "--subscription-id", "sub-1",
                        "--observed-session-id", "actual-chat", "--evidence-ref", "operator:host-probe-receipt"))
    assert observed["operator_verified"] is True
    assert run(args(db, "binding", "b"))["binding"]["ready"] is True
    assert run(args(db, "revoke-route", "a", "b"))["allowed"] is False
    assert run(args(db, "disable", "b"))["active"] is False


def test_companion_registers_coordination_without_machine_execution(tmp_path, monkeypatch):
    captured = {}
    real_build = service.build_oauth_server
    def build(**kw):
        server = real_build(**kw)
        captured["server"] = server
        return server
    monkeypatch.setattr(service, "build_oauth_server", build)
    allowed = tmp_path / "allowed.json"
    allowed.write_text('["client"]')
    for key, value in {
        "ASTER_MCP_PUBLIC_ORIGIN": "https://companion.invalid",
        "ASTER_MCP_OAUTH_DB": tmp_path / "oauth.sqlite",
        "ASTER_COORDINATION_DB": tmp_path / "coord.sqlite",
        "ASTER_ADMIN_CLIENTS": allowed,
        "ASTER_COORDINATION_NATIVE_EVENTS": "0",
    }.items():
        monkeypatch.setenv(key, str(value))
    service.create_app()
    server = captured["server"]
    tool_names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "aster_delegate_task" in tool_names and "aster_claim_task" in tool_names
    assert not {"run_command", "start_job", "execute_command", "begin_upload"} & tool_names
    # Assert the exact composition, not a stale count predating the panel.
    assert tool_names == {
        "connection_status", "aster_profile", "aster_list_agents",
        "aster_delegate_task", "aster_inbox", "aster_outcomes",
        "aster_claim_task", "aster_renew_lease", "aster_checkpoint_task",
        "aster_complete_task", "aster_task_status", "aster_cancel_task",
        "aster_reconcile_task", "aster_verify_result",
        "aster_panel", "aster_panel_refresh", "aster_panel_task",
    }
    with caller("client"):
        status = invoke(server, "connection_status")
        assert status["admin_approved"] and not status["machine_execution"]


def test_companion_rejects_shared_state_before_any_database_created(tmp_path, monkeypatch):
    same = tmp_path / "state"
    for key in ("ASTER_MCP_OAUTH_DB", "ASTER_COORDINATION_DB", "ASTER_ADMIN_CLIENTS"):
        monkeypatch.setenv(key, str(same))
    monkeypatch.setenv("ASTER_MCP_PUBLIC_ORIGIN", "https://companion.invalid")
    with pytest.raises(ValueError, match="separate state"):
        service.create_app()
    assert not same.exists()


def test_sender_pull_reenrollment_preserves_outcomes_and_other_worker(operator):
    coord, _, _, client, enroll = operator
    run(args(coord, *enroll, "--receive-mode", "native"))
    store = Store(coord)
    worker_client = "worker-client"
    store.register_agent(agent_id="worker", client_id=worker_client,
                         display_name="Worker", provider="fixture", receive_mode="native")
    store.grant(sender="researcher", receiver="worker")
    store.bind_receiver(agent_id="worker", subscription_id="worker-subscription",
                        expected_session_id="worker-session", expires_at=store.clock() + 3600)
    store.verify_receiver(agent_id="worker", subscription_id="worker-subscription",
                          observed_session_id="worker-session", evidence_ref="fixture:observed-probe")

    def assign(request):
        task = store.delegate_task(client, to_agent="worker", request_id=request,
                                   instruction="Fixture only", expected_result="Fixture result")
        claim = store.claim_task(worker_client, task_id=task["task_id"],
                                 request_id="claim-" + request, lease_seconds=3600)
        return task, {key: claim[key] for key in ("lease_token", "fence")}

    completed, ownership = assign("completed")
    result = store.complete_task(worker_client, task_id=completed["task_id"],
                                 **ownership, result={"fixture": True})
    store.verify_result(client, task_id=completed["task_id"], expected_revision=result["revision"],
                        accepted=True, evidence={"checks": ["fixture checked"], "reference": "fixture:result"})
    running, _ = assign("running")
    outcomes = store.outcomes(client)
    worker_profile = store.profile(worker_client)
    binding = store.native_binding("worker")
    task_states = [store.task_status(client, task_id=t["task_id"]) for t in (completed, running)]
    with sqlite3.connect(coord) as db:
        grants = db.execute("SELECT * FROM coord_grants ORDER BY sender,receiver").fetchall()

    changed = run(args(coord, *enroll, "--receive-mode", "pull", "--replace"))["agent"]
    assert changed["generation"] == 2
    assert changed["receive_mode"] == "pull" and changed["receive_ready"] is True
    assert changed["native_binding_status"] == "not_configured"
    reopened = Store(coord)
    assert reopened.profile(client)["receive_mode"] == "pull"
    assert reopened.outcomes(client) == outcomes
    assert reopened.profile(worker_client) == worker_profile
    assert reopened.native_binding("worker") == binding
    assert [reopened.task_status(client, task_id=t["task_id"]) for t in (completed, running)] == task_states
    with sqlite3.connect(coord) as db:
        assert db.execute("SELECT * FROM coord_grants ORDER BY sender,receiver").fetchall() == grants
