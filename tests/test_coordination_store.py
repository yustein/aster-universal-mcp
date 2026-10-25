"""State-machine acceptance against real SQLite, independent connections and restarts."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from coordination.store import CoordinationError, Store


@pytest.fixture
def state(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / "coord.sqlite", clock=lambda: now[0])
    for aid, provider in (("aster", "openai"), ("claude", "anthropic"), ("outsider", "other")):
        store.register_agent(agent_id=aid, client_id=aid + "-client",
                             display_name=aid.title(), provider=provider)
    store.grant(sender="aster", receiver="claude")
    return store, now


def assignment(store, request="send-1", **kwargs):
    return store.delegate_task("aster-client", to_agent="claude", request_id=request,
                               instruction="Count the supplied entries.",
                               expected_result="An object containing count=3.",
                               inputs={"entries": [1, 2, 3]}, **kwargs)


def claim(store, task=None, request="claim-1", **kwargs):
    return store.claim_task("claude-client", request_id=request,
                           task_id=task["task_id"] if task else None, **kwargs)


def credentials(claimed):
    return {"task_id": claimed["task"]["task_id"], "lease_token": claimed["lease_token"],
            "fence": claimed["fence"]}


def test_cross_provider_roundtrip_checkpoint_result_and_independent_review(state):
    store, _ = state
    task = assignment(store)
    assert task["status"] == "queued"
    c = claim(store, task)
    cp = store.checkpoint_task("claude-client", **credentials(c),
                               expected_revision=c["task"]["revision"], checkpoint={"read": 3})
    assert cp["revision"] == 2
    done = store.complete_task("claude-client", **credentials(c), result={"count": 3},
                              evidence={"input_count": 3})
    assert done["status"] == "completed" and done["acceptance"] == "pending"
    assert done["result_verification"] == "worker_reported"
    verified = store.verify_result("aster-client", task_id=task["task_id"],
                                   expected_revision=done["revision"], accepted=True,
                                   evidence={"checks": ["Independently counted three input entries"],
                                             "reference": "test:known-input"})
    assert verified["result_verification"] == "sender_verified"
    assert verified["acceptance"] == "accepted"
    assert store.task_status("aster-client", task_id=task["task_id"]) == verified
    events = store.events_after()
    assert any(e["recipient"] == "aster" and e["event_type"] == "task.completed" for e in events)
    assert c["lease_token"] not in json.dumps(events)
    assert "Count the supplied" not in json.dumps(events)
    assert "client" not in json.dumps(events)


def test_authenticating_as_another_agent_does_not_grant_read_or_claim(state):
    store, _ = state
    task = assignment(store)
    assert [a["agent_id"] for a in store.list_agents("outsider-client")["agents"]] == ["outsider"]
    for client in ("outsider-client", "unknown-client"):
        with pytest.raises(CoordinationError):
            store.task_status(client, task_id=task["task_id"])
        with pytest.raises(CoordinationError):
            store.claim_task(client, request_id="wrong-claim", task_id=task["task_id"])
    with pytest.raises(CoordinationError, match="ROUTE_NOT_ALLOWED"):
        store.delegate_task("claude-client", to_agent="aster", request_id="no-grant",
                            instruction="Read", expected_result="Reply")
    with pytest.raises(CoordinationError, match="TASK_NOT_ACCESSIBLE"):
        store.cancel_task("claude-client", task_id=task["task_id"], reason="Not the sender")


def test_request_identity_is_sender_scoped_and_payload_bound(state):
    store, _ = state
    task = assignment(store)
    assert assignment(store) == task
    with pytest.raises(CoordinationError, match="IDEMPOTENCY_CONFLICT"):
        assignment(store, authorized_scope={"changed": True})
    store.grant(sender="outsider", receiver="claude")
    second = store.delegate_task("outsider-client", to_agent="claude", request_id="send-1",
                                 instruction="Different sender", expected_result="Reply")
    assert second["task_id"] != task["task_id"]
    assert len(store.inbox("claude-client")["tasks"]) == 2


def test_two_independent_database_connections_cannot_claim_same_task(state):
    store, now = state
    task = assignment(store)
    other = Store(store.path, clock=lambda: now[0])
    barrier = threading.Barrier(2)
    def attempt(candidate, request):
        barrier.wait(timeout=5)
        try:
            return candidate.claim_task("claude-client", task_id=task["task_id"], request_id=request)
        except CoordinationError as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(attempt, candidate, request)
                   for candidate, request in ((store, "one"), (other, "two"))]
        results = [future.result(timeout=10) for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "TASK_NOT_QUEUED" in results


def test_lost_claim_reply_recovers_exact_lease_and_cannot_claim_new_work(state):
    store, now = state
    task = assignment(store)
    first = claim(store, task)
    reopened = Store(store.path, clock=lambda: now[0])
    assert claim(reopened, task) == first
    with pytest.raises(CoordinationError, match="IDEMPOTENCY_CONFLICT"):
        claim(store, task, lease_seconds=30)
    now[0] += 300
    with pytest.raises(CoordinationError, match="CLAIM_NO_LONGER_CURRENT"):
        claim(reopened, task)
    assert reopened.task_status("aster-client", task_id=task["task_id"])["status"] == "uncertain"


def test_empty_claim_retry_never_silently_becomes_new_execution(state):
    store, _ = state
    assert claim(store) == {"task": None}
    assignment(store)
    assert claim(store) == {"task": None}
    assert claim(store, request="claim-new")["task"] is not None


def test_lease_expiry_is_uncertain_and_stale_worker_is_fenced_after_explicit_retry(state):
    store, now = state
    task = assignment(store)
    first = claim(store, task, lease_seconds=15)
    now[0] += 15  # Expiration is inclusive at the exact boundary.
    uncertain = store.task_status("aster-client", task_id=task["task_id"])
    assert uncertain["status"] == "uncertain"
    assert store.inbox("claude-client")["tasks"] == []
    with pytest.raises(CoordinationError, match="RETRY_REQUIRES_EVIDENCE"):
        store.reconcile_task("aster-client", task_id=task["task_id"], decision="retry",
                             evidence={"guess": True}, expected_revision=uncertain["revision"])
    store.reconcile_task("aster-client", task_id=task["task_id"], decision="retry",
                         evidence={"effects_absent": True, "reference": "no-op executor inspected"},
                         expected_revision=uncertain["revision"])
    current = claim(store, task, request="claim-2")
    assert current["fence"] > first["fence"]
    operations = [
        lambda: store.renew_lease("claude-client", **credentials(first)),
        lambda: store.checkpoint_task("claude-client", **credentials(first),
                                      expected_revision=first["task"]["revision"], checkpoint={"bad": 1}),
        lambda: store.complete_task("claude-client", **credentials(first), result={"wrong": 1}),
    ]
    for operation in operations:
        with pytest.raises(CoordinationError, match="STALE_LEASE"):
            operation()


def test_completion_retry_is_immutable_and_survives_restart_and_lease_expiry(state):
    store, now = state
    task = assignment(store)
    c = claim(store, task)
    done = store.complete_task("claude-client", **credentials(c), result={"count": 3})
    now[0] += 301
    reopened = Store(store.path, clock=lambda: now[0])
    assert reopened.complete_task("claude-client", **credentials(c), result={"count": 3}) == done
    with pytest.raises(CoordinationError, match="RESULT_CONFLICT"):
        reopened.complete_task("claude-client", **credentials(c), result={"count": 4})
    assert claim(reopened, request="next-claim")["task"] is None


def test_binding_replacement_invalidates_old_identity_and_inflight_lease(state):
    store, _ = state
    task = assignment(store)
    c = claim(store, task)
    previous_generation = store.profile("claude-client")["generation"]
    store.register_agent(agent_id="claude", client_id="replacement-client", display_name="Replacement",
                         provider="anthropic", replace=True)
    assert store.profile("replacement-client")["generation"] > previous_generation
    with pytest.raises(CoordinationError, match="AGENT_NOT_ENROLLED"):
        store.complete_task("claude-client", **credentials(c), result={"count": 3})
    with pytest.raises(CoordinationError, match="STALE_LEASE"):
        store.complete_task("replacement-client", **credentials(c), result={"count": 3})
    assert store.task_status("aster-client", task_id=task["task_id"])["status"] == "uncertain"


def test_native_receiver_requires_matching_observed_evidence_and_current_subscription(state):
    store, now = state
    store.register_agent(agent_id="claude", client_id="claude-client", display_name="Native",
                         provider="openai", receive_mode="native", replace=True)
    task = assignment(store)
    binding = store.bind_receiver(agent_id="claude", subscription_id="sub-1",
                                  expected_session_id="intended-chat", expires_at=1100)
    assert not store.native_binding("claude")["ready"]
    with pytest.raises(CoordinationError, match="RECEIVER_NOT_READY"):
        claim(store, task)
    with pytest.raises(CoordinationError, match="WRONG_RECEIVER"):
        store.verify_receiver(agent_id="claude", subscription_id="sub-1",
                              observed_session_id="wrong-chat", evidence_ref="actual host transcript")
    with pytest.raises(CoordinationError, match="INVALID_EVIDENCE"):
        store.verify_receiver(agent_id="claude", subscription_id="sub-1", observed_session_id="intended-chat",
                              evidence_ref="caller declaration", evidence_source="model_asserted")
    store.verify_receiver(agent_id="claude", subscription_id="sub-1", observed_session_id="intended-chat",
                          evidence_ref="operator inspected actual receiver probe")
    assert store.native_binding("claude")["ready"]
    c = claim(store, task)
    renewed = store.bind_receiver(agent_id="claude", subscription_id="sub-1",
                                  expected_session_id="intended-chat", expires_at=1200)
    assert renewed["generation"] == binding["generation"] and renewed["operator_verified"]
    now[0] = 1200
    assert store.task_status("aster-client", task_id=task["task_id"])["status"] == "uncertain"
    with pytest.raises(CoordinationError):
        store.renew_lease("claude-client", **credentials(c))
    refreshed = store.bind_receiver(agent_id="claude", subscription_id="sub-1",
                                    expected_session_id="intended-chat", expires_at=1300)
    assert refreshed["generation"] > renewed["generation"] and not refreshed["operator_verified"]


def test_native_reconnect_recovers_queue_and_saved_result_notifications(state):
    store, _ = state
    task = assignment(store)
    pending = store.pending_task_events("claude")
    assert len(pending) == 1 and pending[0]["task_id"] == task["task_id"]
    c = claim(store, task)
    assert store.pending_task_events("claude") == []
    store.complete_task("claude-client", **credentials(c), result={"count": 3})
    receipt = store.pending_task_events("aster")
    assert len(receipt) == 1 and receipt[0]["event_type"] == "task.completed"
    assert receipt[0]["recipient"] == "aster"


def test_disconnected_result_backlog_can_be_drained_in_bounded_durable_pages(state):
    store, now = state
    task_ids = []
    for index in range(5):
        task = assignment(store, request=f"send-{index}")
        c = claim(store, task, request=f"claim-{index}")
        store.complete_task("claude-client", **credentials(c), result={"count": 3})
        task_ids.append(task["task_id"])
    after = 0
    seen = []
    for expected_count in (2, 2, 1):
        reopened = Store(store.path, clock=lambda: now[0])
        page = reopened.pending_task_events("aster", after=after, limit=2)
        assert len(page) == expected_count
        assert all(event["sequence"] > after for event in page)
        seen.extend(event["task_id"] for event in page)
        after = page[-1]["sequence"]
    assert seen == task_ids
    assert store.pending_task_events("aster", after=after, limit=2) == []
    with pytest.raises(CoordinationError, match="INVALID_INPUT"):
        store.pending_task_events("aster", after=-1, limit=2)


def test_cancel_cannot_undo_running_work_or_allow_automatic_retry(state):
    store, _ = state
    queued = assignment(store)
    assert store.cancel_task("aster-client", task_id=queued["task_id"], reason="Stop")["status"] == "cancelled"
    active = assignment(store, request="active")
    c = claim(store, active)
    cancellation = store.cancel_task("aster-client", task_id=active["task_id"], reason="Stop")
    assert cancellation["status"] == "uncertain" and cancellation["cancel_requested"]
    with pytest.raises(CoordinationError, match="STALE_LEASE"):
        store.complete_task("claude-client", **credentials(c), result={"count": 3})
    with pytest.raises(CoordinationError, match="RETRY_NOT_ALLOWED"):
        store.reconcile_task("aster-client", task_id=active["task_id"], decision="retry",
                             expected_revision=cancellation["revision"], evidence={"effects_absent": True})


def test_checkpoint_uses_optimistic_revision_and_retry_does_not_duplicate_progress(state):
    store, _ = state
    task = assignment(store)
    c = claim(store, task)
    args = dict(**credentials(c), expected_revision=c["task"]["revision"], checkpoint={"position": 1})
    first = store.checkpoint_task("claude-client", **args)
    assert store.checkpoint_task("claude-client", **args) == first
    with pytest.raises(CoordinationError, match="REVISION_CONFLICT"):
        store.checkpoint_task("claude-client", **{**args, "checkpoint": {"position": 2}})


@pytest.mark.parametrize("initial,changed", [(True, 1), (1, True), (1, 1.0)])
def test_checkpoint_retry_distinguishes_json_booleans_integers_and_floats(state, initial, changed):
    store, _ = state
    c = claim(store, assignment(store))
    args = dict(**credentials(c), expected_revision=c["task"]["revision"])
    saved = store.checkpoint_task("claude-client", **args, checkpoint={"value": initial})
    with pytest.raises(CoordinationError, match="REVISION_CONFLICT"):
        store.checkpoint_task("claude-client", **args, checkpoint={"value": changed})
    assert store.task_status("aster-client", task_id=c["task"]["task_id"]) == saved


def test_lost_running_cancellation_reply_preserves_revision_and_receipt_across_restart(state):
    store, now = state
    task = assignment(store)
    claim(store, task)
    arguments = {"task_id": task["task_id"], "reason": "Stop this assignment"}
    cancelled = store.cancel_task("aster-client", **arguments)
    events = store.events_after()
    reopened = Store(store.path, clock=lambda: now[0])
    assert reopened.cancel_task("aster-client", **arguments) == cancelled
    assert reopened.events_after() == events
    # The revision returned by the first response remains usable even if that
    # response was delayed until after its idempotent retry.
    final = reopened.reconcile_task("aster-client", task_id=task["task_id"], decision="cancel",
                                    expected_revision=cancelled["revision"],
                                    evidence={"reference": "Executor confirmed stopped"})
    assert final["status"] == "cancelled"


def test_event_persistence_failure_rolls_back_task_creation_and_completion(state, monkeypatch):
    store, _ = state
    def event_failure(*args, **kwargs):
        raise OSError("simulated event persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_event", event_failure)
        with pytest.raises(OSError):
            assignment(store)
    assert store.inbox("claude-client")["tasks"] == []
    assert store.events_after() == []
    task = assignment(store)  # The failed attempt did not consume request_id.
    c = claim(store, task)
    events = store.events_after()
    with monkeypatch.context() as patch:
        patch.setattr(store, "_event", event_failure)
        with pytest.raises(OSError):
            store.complete_task("claude-client", **credentials(c), result={"count": 3})
    current = store.task_status("aster-client", task_id=task["task_id"])
    assert current["status"] == "running" and current["result"] is None
    assert current["revision"] == c["task"]["revision"]
    assert store.events_after() == events
    assert store.complete_task("claude-client", **credentials(c), result={"count": 3})["status"] == "completed"


def test_worker_cannot_self_verify_or_mark_delivery_as_sender_acceptance(state):
    store, _ = state
    task = assignment(store)
    c = claim(store, task)
    done = store.complete_task("claude-client", **credentials(c), result={"count": 3})
    evidence = {"checks": ["callback accepted"], "reference": "not sufficient"}
    with pytest.raises(CoordinationError, match="TASK_NOT_ACCESSIBLE"):
        store.verify_result("claude-client", task_id=task["task_id"], expected_revision=done["revision"],
                            accepted=True, evidence=evidence)
    with pytest.raises(CoordinationError, match="INVALID_EVIDENCE"):
        store.verify_result("aster-client", task_id=task["task_id"], expected_revision=done["revision"],
                            accepted=True, evidence={"callback": 200})


@pytest.mark.parametrize("bad", [float("nan"), {"bad": object()}, {1: "key"}, ["not object"], {"data": "x" * 70000}])
def test_bad_or_unbounded_json_is_rejected_before_queuing(state, bad):
    store, _ = state
    with pytest.raises(CoordinationError, match="INVALID_INPUT"):
        store.delegate_task("aster-client", to_agent="claude", request_id="bad",
                            instruction="Read", expected_result="Reply", inputs=bad)
    assert store.inbox("claude-client")["tasks"] == []


@pytest.mark.parametrize("lease", [True, 14, 3601, 15.0, "30"])
def test_lease_type_and_bounds_are_not_coerced(state, lease):
    store, _ = state
    assignment(store)
    with pytest.raises(CoordinationError, match="INVALID_INPUT"):
        claim(store, lease_seconds=lease)


def test_expired_unclaimed_task_is_not_executed(state):
    store, now = state
    task = assignment(store, ttl_seconds=60)
    now[0] += 60
    assert store.inbox("claude-client")["tasks"] == []
    assert store.task_status("aster-client", task_id=task["task_id"])["status"] == "expired"


def test_deadline_crossing_after_maintenance_cannot_return_an_expired_new_claim(state, monkeypatch):
    store, now = state
    task = assignment(store, ttl_seconds=60)
    now[0] = task["deadline"] - 1
    original_expire = store._expire
    calls = [0]
    def advance_after_scan(db):
        original_expire(db)
        calls[0] += 1
        if calls[0] == 2:
            now[0] = task["deadline"] + 1
    monkeypatch.setattr(store, "_expire", advance_after_scan)
    with pytest.raises(CoordinationError, match="TASK_NOT_QUEUED"):
        claim(store, task)
    assert store.task_status("aster-client", task_id=task["task_id"])["status"] == "expired"
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM coord_claims").fetchone()[0] == 0


def test_lease_crossing_after_maintenance_cannot_replay_an_expired_claim(state, monkeypatch):
    store, now = state
    task = assignment(store)
    c = claim(store, task, lease_seconds=15)
    now[0] = c["lease_expires_at"] - 1
    original_expire = store._expire
    calls = [0]
    def advance_after_scan(db):
        original_expire(db)
        calls[0] += 1
        if calls[0] == 2:
            now[0] = c["lease_expires_at"] + 1
    monkeypatch.setattr(store, "_expire", advance_after_scan)
    with pytest.raises(CoordinationError, match="CLAIM_NO_LONGER_CURRENT"):
        claim(store, task, lease_seconds=15)
    assert store.task_status("aster-client", task_id=task["task_id"])["status"] == "uncertain"


def test_renewal_cannot_resurrect_lease_expiring_during_ownership_check(state, monkeypatch):
    store, now = state
    c = claim(store, assignment(store), lease_seconds=15)
    now[0] = c["lease_expires_at"] - 1
    original_lease = store._lease
    def advance_after_check(*args, **kwargs):
        result = original_lease(*args, **kwargs)
        now[0] = c["lease_expires_at"] + 1
        return result
    monkeypatch.setattr(store, "_lease", advance_after_check)
    with pytest.raises(CoordinationError, match="STALE_LEASE"):
        store.renew_lease("claude-client", **credentials(c))
    assert store.task_status("aster-client", task_id=c["task"]["task_id"])["status"] == "uncertain"


def test_generation_and_client_enrollment_conflicts_are_explicit(state):
    store, _ = state
    with pytest.raises(CoordinationError, match="CLIENT_ALREADY_ENROLLED"):
        store.register_agent(agent_id="another", client_id="aster-client", display_name="Another", provider="other")
    with pytest.raises(CoordinationError, match="AGENT_EXISTS"):
        store.register_agent(agent_id="aster", client_id="new-client", display_name="Another", provider="other")


def test_private_database_and_no_runtime_secrets_in_public_task_views(state):
    store, _ = state
    c = claim(store, assignment(store))
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o077 == 0
    assert c["lease_token"] not in json.dumps(c["task"])
    assert all(not key.startswith("_") for key in c["task"])


def test_process_restart_preserves_inflight_ownership_without_requeue(state):
    store, now = state
    task = assignment(store)
    c = claim(store, task)
    reopened = Store(store.path, clock=lambda: now[0])
    assert reopened.inbox("claude-client")["tasks"] == []
    assert reopened.task_status("aster-client", task_id=task["task_id"])["status"] == "running"
    assert reopened.renew_lease("claude-client", **credentials(c))["fence"] == c["fence"]


def test_existing_application_database_cannot_become_coordination_state(tmp_path):
    path = tmp_path / "other-application.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE existing_application (value TEXT)")
        db.execute("INSERT INTO existing_application VALUES ('preserve')")
    path.chmod(0o600)
    with pytest.raises(CoordinationError, match="STATE_NAMESPACE_CONFLICT"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("existing_application",)]
        assert db.execute("SELECT value FROM existing_application").fetchone()[0] == "preserve"


def test_future_schema_is_rejected_without_creating_current_tables(tmp_path):
    path = tmp_path / "future.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE coord_meta(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO coord_meta VALUES ('schema','2')")
    path.chmod(0o600)
    with pytest.raises(CoordinationError, match="Unsupported coordination database schema"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("coord_meta",)]
