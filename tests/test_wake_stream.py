import os

from starlette.applications import Starlette
from starlette.testclient import TestClient

from coordination.store import Store
from coordination.wake_stream import EventSignal, WakeStream


def test_notifier_fires_once_after_committed_event_transaction(tmp_path):
    calls = []
    store = Store(tmp_path / "coord.sqlite", event_notifier=lambda: calls.append("notified"))
    store.register_agent(agent_id="a", client_id="a-client", display_name="A", provider="test")
    store.register_agent(agent_id="b", client_id="b-client", display_name="B", provider="test")
    store.grant(sender="a", receiver="b")
    store.delegate_task("a-client", to_agent="b", request_id="send-1",
                        instruction="fixture", expected_result="fixture")
    assert calls == ["notified"]


def test_wake_stream_sends_only_event_metadata(tmp_path):
    signal = EventSignal()
    store = Store(tmp_path / "coord.sqlite", event_notifier=signal.notify)
    store.register_agent(agent_id="a", client_id="a-client", display_name="A", provider="test")
    store.register_agent(agent_id="b", client_id="b-client", display_name="B", provider="test")
    store.grant(sender="a", receiver="b")
    secret = tmp_path / "wake-relay.secret"
    secret.write_text("x" * 48)
    os.chmod(secret, 0o600)
    app = WakeStream(Starlette(), store_getter=lambda: store, signal=signal,
                     secret_path=secret, public_host="coord.example.test")
    client = TestClient(app)
    headers = {"authorization": "Bearer " + "x" * 48,
               "host": "coord.example.test", "x-forwarded-proto": "https"}
    with client.websocket_connect("/wake-stream?after=0", headers=headers) as ws:
        store.delegate_task("a-client", to_agent="b", request_id="send-1",
                            instruction="SECRET INSTRUCTION", expected_result="SECRET RESULT")
        first = ws.receive_json()
        second = ws.receive_json()
        assert [first["event"]["event_type"], second["event"]["event_type"]] == ["task.updated", "task.queued"]
        raw = str((first, second))
        assert "SECRET INSTRUCTION" not in raw and "SECRET RESULT" not in raw
        assert second["event"]["recipient"] == "b"
