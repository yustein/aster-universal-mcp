"""Native callback transport and coordination boundaries, without live providers."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from pathlib import Path
import socket
import sqlite3
import sys
import time

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client_registry import ClientRegistry
from coordination.native import (
    CALLBACK_TIMEOUT, EVENT_NAME, MAX_RESPONSE_BYTES, NativeEvents,
    CallbackError, callback_url, install_native_events, public_address,
    public_https_post, signature_headers, signing_key,
)
from coordination.store import CoordinationError, Store
from oauth_runtime import build_oauth_server
from token_store import TokenStore
from mcp.server.transport_security import TransportSecuritySettings


# Independently generated with the recovered bridge's standardwebhooks 1.1.1.
SECRET = "whsec_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
SECOND_SECRET = "whsec_" + base64.b64encode(b"B" * 32).decode()
ORIGIN = "https://mcp.example.invalid"


class Callback:
    def __init__(self):
        self.calls = []
        self.statuses = []
        self.bad_challenge = False

    async def __call__(self, url, body, headers):
        event = json.loads(body)
        self.calls.append((url, event, dict(headers), body))
        if event.get("type") == "verification":
            return 200, json.dumps({"challenge": "wrong" if self.bad_challenge else event["challenge"]}).encode()
        return (self.statuses.pop(0) if self.statuses else 200), b""

    def events(self):
        return [call for call in self.calls if call[1].get("type") != "verification"]


@pytest.fixture
def env(tmp_path):
    clock = [time.time()]
    store = Store(tmp_path / "coordination.sqlite", clock=lambda: clock[0])
    oauth = tmp_path / "oauth.sqlite"
    clients = ClientRegistry(str(oauth))
    for client in ("source-client", "worker-client"):
        clients.register_external(client, "Test client", ["https://example.invalid/callback"])
    allowlist = tmp_path / "allowed.json"
    allowlist.write_text(json.dumps(["source-client", "worker-client"]))
    for agent, client in (("source", "source-client"), ("worker", "worker-client")):
        store.register_agent(agent_id=agent, client_id=client, display_name="Test " + agent,
                             provider="test-provider", receive_mode="native")
    store.grant(sender="source", receiver="worker")
    post = Callback()
    native = NativeEvents(store, allowlist_path=allowlist, oauth_db_path=oauth, post=post)
    return {"store": store, "native": native, "clock": clock, "post": post,
            "allowlist": allowlist, "oauth": oauth, "clients": clients}


def params(agent="worker", *, secret=SECRET, url=None):
    return {"name": EVENT_NAME,
            "arguments": {"agent_id": agent, "expected_session_id": "session-" + agent},
            "delivery": {"mode": "webhook", "url": url or "https://callback.example.invalid/" + agent,
                         "secret": secret}, "cursor": None}


def subscribe(env, agent="worker", **kwargs):
    return asyncio.run(env["native"].subscribe(agent + "-client", params(agent, **kwargs)))


def verify(env, subscription, agent="worker"):
    return env["store"].verify_receiver(agent_id=agent, subscription_id=subscription["id"],
        observed_session_id="session-" + agent, evidence_ref="isolated operator fixture")


def delegate(env, request="assignment-1"):
    return env["store"].delegate_task("source-client", to_agent="worker", request_id=request,
        instruction="PRIVATE instruction never included in callback", expected_result="A result document",
        inputs={"private": "PRIVATE input never included in callback"})


def test_standard_webhooks_signature_matches_independent_library_vector():
    body = b'{"type":"verification","challenge":"fixed-test-challenge"}'
    headers = signature_headers(SECRET, "subscription_fixture", "verification_fixture", body, timestamp=1700000000)
    assert headers["webhook-signature"] == "v1,1hmSu23DMfHFnGZwK3FMM+ulluRIGOXHUHmMn4hRMaY="
    assert headers["webhook-id"] == "verification_fixture"
    assert headers["webhook-timestamp"] == "1700000000"
    assert signing_key(SECRET) == bytes(range(32))
    for invalid in ("short", "whsec_YQ==", "whsec_not-base64-", SECRET + "$"):
        with pytest.raises(CallbackError):
            signing_key(invalid)


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com/", "https://@example.com/",
    "https://example.com/#fragment", "https://example.com:0/", "https://example.com:65536/",
    "https://example.com/\r\ninject", "https://example.com\\@127.0.0.1/"])
def test_callback_urls_reject_unsafe_framing(url):
    with pytest.raises(CallbackError):
        callback_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "224.0.0.1",
    "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "2001:db8::1", "fe80::1%eth0"])
def test_private_reserved_and_multicast_addresses_are_not_callbacks(address):
    assert not public_address(address)


class Writer:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def transport_fixture(response, *, addresses=None):
    resolved = []
    connected = []
    writer = Writer()

    async def resolver(host, port, **kwargs):
        resolved.append((host, port))
        return addresses or [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    async def connector(**kwargs):
        connected.append(kwargs)
        reader = asyncio.StreamReader()
        reader.feed_data(response)
        reader.feed_eof()
        return reader, writer

    return resolver, connector, resolved, connected, writer


def test_https_is_pinned_to_validated_ip_preserves_tls_name_and_handles_chunking():
    response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
    resolver, connector, resolved, connected, writer = transport_fixture(response)
    result = asyncio.run(public_https_post("https://Callback.Example:444/hook?q=1", b"{}",
        {"Content-Type": "application/json", "Host": "attacker.invalid", "Content-Length": "999"},
        resolver=resolver, connector=connector))
    assert result == (200, b"{}")
    assert resolved == [("callback.example", 444)]
    assert connected[0]["host"] == "8.8.8.8"
    assert connected[0]["server_hostname"] == "callback.example"
    assert connected[0]["ssl"].check_hostname
    assert b"POST /hook?q=1 HTTP/1.1" in writer.data
    assert b"Host: callback.example:444" in writer.data
    assert b"attacker.invalid" not in writer.data and b"999" not in writer.data
    assert writer.closed


def test_mixed_public_private_dns_fails_before_connecting_and_redirects_are_not_followed():
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ("8.8.8.8", "127.0.0.1")]
    resolver, connector, _, connected, _ = transport_fixture(b"", addresses=answers)
    with pytest.raises(CallbackError, match="blocked_address"):
        asyncio.run(public_https_post("https://callback.example/", b"{}", {}, resolver=resolver, connector=connector))
    assert not connected
    resolver, connector, _, connected, _ = transport_fixture(
        b"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1/\r\nContent-Length: 0\r\n\r\n")
    assert asyncio.run(public_https_post("https://callback.example/", b"{}", {},
        resolver=resolver, connector=connector)) == (302, b"")
    assert len(connected) == 1


def test_callback_response_size_and_total_timeout_are_bounded():
    body = b"x" * (MAX_RESPONSE_BYTES + 1)
    response = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    resolver, connector, _, _, writer = transport_fixture(response)
    with pytest.raises(CallbackError, match="response_too_large"):
        asyncio.run(public_https_post("https://callback.example/", b"{}", {}, resolver=resolver, connector=connector))
    assert writer.closed

    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    with pytest.raises(CallbackError, match="timeout"):
        asyncio.run(public_https_post("https://callback.example/", b"{}", {}, timeout=0.01, resolver=stalled))


def test_failed_challenge_and_wrong_identity_cannot_create_binding(env):
    env["post"].bad_challenge = True
    with pytest.raises(CallbackError, match="challenge_failed"):
        subscribe(env)
    assert env["store"].native_binding("worker") is None
    before = len(env["post"].calls)
    with pytest.raises(CoordinationError, match="NATIVE_NOT_AUTHORIZED"):
        asyncio.run(env["native"].subscribe("source-client", params()))
    assert len(env["post"].calls) == before


def test_probe_precedes_host_verification_and_transport_never_completes_task(env):
    task = delegate(env)
    subscription = subscribe(env)
    assert not subscription["bindingVerified"]
    asyncio.run(env["native"].run_once())
    events = env["post"].events()
    assert len(events) == 1
    assert events[0][1]["data"]["event_type"] == "receiver.probe"
    assert events[0][1]["data"]["task_id"] is None
    with pytest.raises(CoordinationError, match="RECEIVER_NOT_READY"):
        env["store"].claim_task("worker-client", request_id="before-verification")
    with pytest.raises(CoordinationError, match="WRONG_RECEIVER"):
        env["store"].verify_receiver(agent_id="worker", subscription_id=subscription["id"],
            observed_session_id="wrong-session", evidence_ref="wrong fixture")
    verify(env, subscription)
    asyncio.run(env["native"].run_once())
    notice = env["post"].events()[-1]
    assert notice[1]["data"]["task_id"] == task["task_id"]
    assert b"PRIVATE" not in notice[3]
    assert env["store"].task_status("source-client", task_id=task["task_id"])["status"] == "queued"
    receipt = [r for r in env["native"].receipts("worker-client", subscription["id"]) if r["kind"] == "task"][0]
    assert receipt["status"] == "accepted" and receipt["last_status"] == 200


def test_exact_renewal_and_key_rotation_preserve_verified_binding_but_url_change_does_not(env):
    original = subscribe(env)
    verified = verify(env, original)
    renewed = subscribe(env)
    assert renewed["id"] == original["id"] and renewed["bindingVerified"]
    assert env["store"].native_binding("worker")["generation"] == verified["generation"]
    changed_key = subscribe(env, secret=SECOND_SECRET)
    assert changed_key["id"] == original["id"] and changed_key["bindingVerified"]
    assert env["store"].native_binding("worker")["generation"] == verified["generation"]
    reordered = params(secret=SECOND_SECRET)
    reordered["arguments"] = dict(reversed(list(reordered["arguments"].items())))
    assert asyncio.run(env["native"].subscribe("worker-client", reordered))["id"] == original["id"]
    changed_url = subscribe(env, secret=SECOND_SECRET, url="https://elsewhere.example/hook")
    assert changed_url["id"] != changed_key["id"] and not changed_url["bindingVerified"]
    with sqlite3.connect(env["store"].path) as db:
        assert db.execute("SELECT count(*) FROM coord_native_subscriptions WHERE active=1").fetchone()[0] == 1
        assert db.execute("SELECT secret FROM coord_native_subscriptions WHERE id=?", (original["id"],)).fetchone()[0] is None


def test_key_rotation_verifies_new_key_only_preserves_lease_and_bounds_overlap(env):
    original = subscribe(env)
    binding = verify(env, original)
    asyncio.run(env["native"].run_once())
    task = delegate(env)
    claim = env["store"].claim_task("worker-client", request_id="rotation-claim", task_id=task["task_id"])
    env["post"].bad_challenge = True
    with pytest.raises(CallbackError, match="challenge_failed"):
        subscribe(env, secret=SECOND_SECRET)
    failed = env["post"].calls[-1]
    expected = signature_headers(SECOND_SECRET, original["id"], failed[2]["webhook-id"], failed[3],
                                 timestamp=int(failed[2]["webhook-timestamp"]))
    assert failed[2]["webhook-signature"] == expected["webhook-signature"]
    assert env["native"]._subscription(original["id"])["secret"] == SECRET
    assert env["store"].native_binding("worker")["generation"] == binding["generation"]
    env["post"].bad_challenge = False
    rotated = subscribe(env, secret=SECOND_SECRET)
    assert rotated["id"] == original["id"] and rotated["bindingVerified"]
    successful_challenge = env["post"].calls[-1]
    assert " " not in successful_challenge[2]["webhook-signature"]
    rotation_until = env["native"]._subscription(original["id"])["rotation_until"]
    assert rotation_until == env["clock"][0] + 300
    asyncio.run(env["native"].run_once())
    notice = [c for c in env["post"].events() if c[1]["data"]["task_id"] == task["task_id"]][-1]
    signatures = notice[2]["webhook-signature"].split(" ")
    assert len(signatures) == 2
    for secret in (SECRET, SECOND_SECRET):
        expected = signature_headers(secret, original["id"], notice[2]["webhook-id"], notice[3],
                                     timestamp=int(notice[2]["webhook-timestamp"]))
        assert expected["webhook-signature"] in signatures
    # Rotation did not fence the already-running assignment or change its token.
    completed = env["store"].complete_task("worker-client", task_id=task["task_id"],
        lease_token=claim["lease_token"], fence=claim["fence"], result={"summary": "verified test result"})
    assert completed["status"] == "completed"
    env["clock"][0] += 299
    subscribe(env, secret=SECOND_SECRET)
    assert env["native"]._subscription(original["id"])["rotation_until"] == rotation_until
    env["clock"][0] += 2
    later = delegate(env, request="after-rotation")
    asyncio.run(env["native"].run_once())
    notice = [c for c in env["post"].events() if c[1]["data"]["task_id"] == later["task_id"]][-1]
    expected = signature_headers(SECOND_SECRET, original["id"], notice[2]["webhook-id"], notice[3],
                                 timestamp=int(notice[2]["webhook-timestamp"]))
    assert notice[2]["webhook-signature"] == expected["webhook-signature"]
    assert env["native"]._subscription(original["id"])["previous_secret"] is None


def test_session_change_requires_a_different_verified_destination(env):
    original = subscribe(env)
    verify(env, original)
    changed = params()
    changed["arguments"]["expected_session_id"] = "another-session"
    replacement = asyncio.run(env["native"].subscribe("worker-client", changed))
    assert replacement["id"] != original["id"]
    assert replacement["bindingVerified"] is False
    assert env["native"]._subscription(original["id"])["active"] == 0


def test_offline_tasks_are_recovered_after_cursor_has_advanced(env):
    task = delegate(env)
    asyncio.run(env["native"].run_once())  # No subscription yet; consumes the event log cursor.
    subscription = subscribe(env)
    verify(env, subscription)
    asyncio.run(env["native"].run_once())
    assert any(c[1]["data"]["task_id"] == task["task_id"] for c in env["post"].events())


def test_offline_backlog_larger_than_one_page_advances_durable_cursor(env):
    for index in range(101):
        delegate(env, request=f"backlog-{index}")
    asyncio.run(env["native"].run_once())
    asyncio.run(env["native"].run_once())  # Advance the global cursor past the whole offline backlog.
    subscription = subscribe(env)
    with sqlite3.connect(env["store"].path) as db:
        assert db.execute("SELECT count(*) FROM coord_native_outbox WHERE kind='task'").fetchone()[0] == 100
    restarted = NativeEvents(env["store"], allowlist_path=env["allowlist"], oauth_db_path=env["oauth"], post=env["post"])
    asyncio.run(restarted.run_once())
    with sqlite3.connect(env["store"].path) as db:
        assert db.execute("SELECT count(*) FROM coord_native_outbox WHERE kind='task'").fetchone()[0] == 101
        assert db.execute("SELECT catchup_sequence FROM coord_native_subscriptions WHERE id=?", (subscription["id"],)).fetchone()[0] == 101


def test_unverified_receiver_backlog_does_not_block_other_receiver(env):
    subscribe(env, "source")
    env["store"].grant(sender="worker", receiver="source")
    for index in range(101):
        env["store"].delegate_task("worker-client", to_agent="source", request_id=f"blocked-{index}",
            instruction="Wait for enrollment", expected_result="Receipt")
    worker = subscribe(env)
    verify(env, worker)
    task = delegate(env)
    asyncio.run(env["native"].run_once())
    task_notices = [c for c in env["post"].events() if c[1]["data"]["task_id"] is not None]
    assert len(task_notices) == 1 and task_notices[0][1]["data"]["task_id"] == task["task_id"]


def test_offline_result_catchup_uses_same_event_without_result_body(env):
    receiver = subscribe(env)
    verify(env, receiver)
    task = delegate(env)
    claim = env["store"].claim_task("worker-client", request_id="claim-one", task_id=task["task_id"])
    env["store"].complete_task("worker-client", task_id=task["task_id"], lease_token=claim["lease_token"],
        fence=claim["fence"], result={"private": "PRIVATE result"})
    asyncio.run(env["native"].run_once())
    sender = subscribe(env, "source")
    verify(env, sender, "source")
    asyncio.run(env["native"].run_once())
    notices = [c for c in env["post"].events() if c[1]["data"]["agent_id"] == "source" and c[1]["data"]["task_id"] == task["task_id"]]
    assert len(notices) == 1 and notices[0][1]["data"]["event_type"] == "task.completed"
    assert b"PRIVATE" not in notices[0][3]


def test_retries_keep_event_id_and_bytes_across_process_restart(env):
    subscription = subscribe(env)
    verify(env, subscription)
    asyncio.run(env["native"].run_once())  # Drain the enrollment probe.
    task = delegate(env)
    env["post"].statuses = [503, 200]
    asyncio.run(env["native"].run_once())
    env["clock"][0] += 3
    restarted = NativeEvents(env["store"], allowlist_path=env["allowlist"], oauth_db_path=env["oauth"], post=env["post"])
    asyncio.run(restarted.run_once())
    attempts = [c for c in env["post"].events() if c[1]["data"]["task_id"] == task["task_id"]]
    assert len(attempts) == 2
    assert attempts[0][3] == attempts[1][3]
    assert attempts[0][2]["webhook-id"] == attempts[1][2]["webhook-id"]
    assert attempts[0][2]["webhook-timestamp"] != attempts[1][2]["webhook-timestamp"]
    receipt = [r for r in restarted.receipts("worker-client", subscription["id"]) if r["kind"] == "task"][0]
    assert receipt["attempts"] == 2 and receipt["status"] == "accepted"


def test_attempts_are_bounded_and_transport_failure_does_not_fail_assignment(env):
    subscription = subscribe(env)
    verify(env, subscription)
    asyncio.run(env["native"].run_once())
    task = delegate(env)
    env["post"].statuses = [503] * 10
    for _ in range(8):
        asyncio.run(env["native"].run_once())
        env["clock"][0] += 61
    receipt = [r for r in env["native"].receipts("worker-client", subscription["id"]) if r["kind"] == "task"][0]
    assert receipt["attempts"] == 6 and receipt["status"] == "failed"
    assert env["store"].task_status("source-client", task_id=task["task_id"])["status"] == "queued"


@pytest.mark.parametrize("revoke", ["allowlist", "oauth", "agent"])
def test_fresh_authorization_is_checked_for_every_dispatch(env, revoke):
    subscription = subscribe(env)
    verify(env, subscription)
    asyncio.run(env["native"].run_once())
    delegate(env)
    before = len(env["post"].events())
    if revoke == "allowlist":
        env["allowlist"].write_text('["source-client"]')
    elif revoke == "oauth":
        env["clients"].revoke("worker-client")
    else:
        env["store"].disable_agent("worker")
    asyncio.run(env["native"].run_once())
    assert len(env["post"].events()) == before


def test_unsubscribe_is_idempotent_silent_and_clears_secrets(env):
    subscription = subscribe(env)
    verify(env, subscription)
    delegate(env)
    unsubscribe = params()
    del unsubscribe["delivery"]["secret"]
    del unsubscribe["cursor"]
    assert env["native"].unsubscribe("worker-client", unsubscribe) == {}
    assert env["native"].unsubscribe("worker-client", unsubscribe) == {}
    asyncio.run(env["native"].run_once())
    assert not env["post"].events()
    assert not env["store"].native_binding("worker")["ready"]
    with sqlite3.connect(env["store"].path) as db:
        assert db.execute("SELECT secret FROM coord_native_subscriptions WHERE id=?", (subscription["id"],)).fetchone()[0] is None


def test_expired_identity_keeps_id_but_new_epoch_notifies_without_reexecuting_task(env):
    request = {**params(), "ttlMs": 1000}
    original = asyncio.run(env["native"].subscribe("worker-client", request))
    old_binding = verify(env, original)
    task = delegate(env)
    asyncio.run(env["native"].run_once())
    old_notices = [c for c in env["post"].events() if c[1]["data"]["task_id"] == task["task_id"]]
    assert len(old_notices) == 1
    env["clock"][0] += 2
    asyncio.run(env["native"].run_once())
    assert not env["store"].native_binding("worker")["ready"]
    with sqlite3.connect(env["store"].path) as db:
        assert db.execute("SELECT secret FROM coord_native_subscriptions WHERE id=?", (original["id"],)).fetchone()[0] is None
    renewed = subscribe(env)
    assert renewed["id"] == original["id"] and not renewed["bindingVerified"]
    assert env["store"].native_binding("worker")["generation"] > old_binding["generation"]
    assert env["native"]._subscription(original["id"])["delivery_epoch"] == 2
    asyncio.run(env["native"].run_once())
    assert len([c for c in env["post"].events() if c[1]["data"]["task_id"] == task["task_id"]]) == 1
    verify(env, renewed)
    asyncio.run(env["native"].run_once())
    notices = [c for c in env["post"].events() if c[1]["data"]["task_id"] == task["task_id"]]
    assert len(notices) == 2 and notices[0][1]["eventId"] != notices[1][1]["eventId"]
    history = [r for r in env["native"].receipts("worker-client", original["id"]) if r["kind"] == "task"]
    assert {r["delivery_epoch"] for r in history} == {1, 2}
    assert all(r["status"] == "accepted" for r in history)
    saved = env["store"].task_status("source-client", task_id=task["task_id"])
    assert saved["status"] == "queued" and saved["fence"] == 0


def test_two_dispatchers_share_exclusive_delivery_lease(env):
    subscription = subscribe(env)
    verify(env, subscription)
    asyncio.run(env["native"].run_once())
    task = delegate(env)
    calls = []

    async def slow_post(url, body, headers):
        calls.append(json.loads(body))
        await asyncio.sleep(0.02)
        return 200, b""

    one = NativeEvents(env["store"], allowlist_path=env["allowlist"], oauth_db_path=env["oauth"], post=slow_post)
    two = NativeEvents(env["store"], allowlist_path=env["allowlist"], oauth_db_path=env["oauth"], post=slow_post)

    async def run():
        await asyncio.gather(one.run_once(), two.run_once())

    asyncio.run(run())
    assert len(calls) == 1 and calls[0]["data"]["task_id"] == task["task_id"]


def test_http_extension_uses_existing_oauth_headers_and_lifespan(env):
    server = build_oauth_server(db_path=str(env["oauth"]), origin=ORIGIN)
    lifecycle = []

    @asynccontextmanager
    async def previous_lifespan(application):
        lifecycle.append("start")
        yield {"preserved": True}
        lifecycle.append("stop")

    server._lowlevel_server.lifespan = previous_lifespan
    install_native_events(server, env["native"])
    app = server.streamable_http_app(json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=["mcp.example.invalid"], allowed_origins=[ORIGIN]))
    token = TokenStore(str(env["oauth"])).issue(client_id="worker-client", resource=ORIGIN + "/mcp", scope="system:admin")

    async def call():
        async with app.router.lifespan_context(app):
            assert env["native"]._worker is not None
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                async def rpc(method, *, bearer=token, extra_headers=None, params=None):
                    body = {"jsonrpc": "2.0", "id": 1, "method": method,
                        "params": {**(params or {}), "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {"name": "fixture", "version": "1"},
                            "io.modelcontextprotocol/clientCapabilities": {}}}}
                    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28",
                               "Mcp-Method": method, **(extra_headers or {})}
                    if bearer:
                        headers["Authorization"] = "Bearer " + bearer
                    return await client.post("/mcp", json=body, headers=headers)

                assert (await rpc("events/list", bearer=None)).status_code == 401
                response = await rpc("server/discover")
                assert response.status_code == 200, response.text
                assert response.json()["result"]["capabilities"]["events"] == {}
                listed = await rpc("events/list")
                assert listed.json()["result"]["events"][0]["name"] == EVENT_NAME
                assert listed.json()["result"]["resultType"] == "complete"
                mismatch = await rpc("events/list", extra_headers={"Mcp-Method": "events/subscribe"})
                assert mismatch.status_code == 400
                signed_up = await rpc("events/subscribe", params={**params(), "ttlMs": None})
                assert signed_up.json()["result"]["bindingVerified"] is False, signed_up.text
                assert isinstance(signed_up.json()["result"]["refreshBefore"], str)
                assert signed_up.json()["result"]["cursor"] is None
                verify(env, signed_up.json()["result"])
                refreshed = await rpc("events/subscribe", params=params(secret=SECOND_SECRET))
                assert refreshed.json()["result"]["id"] == signed_up.json()["result"]["id"]
                assert refreshed.json()["result"]["bindingVerified"] is True
                unsupported = await rpc("events/subscribe", params={**params(secret=SECOND_SECRET), "cursor": "unsupported-replay-position"})
                assert unsupported.json()["error"]["code"] == -32602
                env["allowlist"].write_text("[]")
                refused = await rpc("events/list")
                assert "error" in refused.json()
        assert env["native"]._worker is None

    asyncio.run(call())
    assert lifecycle == ["start", "stop"]
