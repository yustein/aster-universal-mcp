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
    assert len(tool_names) == 14
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
