"""State-machine acceptance against real SQLite, independent connections and restarts."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from coordination.store import CoordinationError, MAX_OUTCOMES_BYTES, Store


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


@pytest.mark.parametrize("explicit_task", [True, False])
def test_worker_capacity_rejection_is_atomic_and_request_can_retry_after_completion(state, explicit_task):
    store, now = state
    first_task = assignment(store)
    second_task = assignment(store, request="send-2")
    first = claim(store, first_task)
    reopened = Store(store.path, clock=lambda: now[0])
    assert claim(reopened, first_task) == first  # Live replay still returns the exact lease.
    with pytest.raises(CoordinationError, match="IDEMPOTENCY_CONFLICT"):
        claim(reopened, second_task)  # Same request ID, different payload, even at capacity.
    selected = second_task if explicit_task else None
    before = [store.task_status("aster-client", task_id=t["task_id"])
              for t in (first_task, second_task)]
    events_before = store.events_after()
    with sqlite3.connect(store.path) as db:
        claims_before = db.execute("SELECT * FROM coord_claims").fetchall()
    with pytest.raises(CoordinationError) as rejected:
        claim(reopened, selected, request="claim-next")
    assert rejected.value.code == "WORKER_AT_CAPACITY"
    assert str(rejected.value) == "WORKER_AT_CAPACITY: Receiver already has a running assignment."
    assert [store.task_status("aster-client", task_id=t["task_id"])
            for t in (first_task, second_task)] == before
    assert store.events_after() == events_before
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT * FROM coord_claims").fetchall() == claims_before
    assert claim(reopened, first_task) == first
    done = store.complete_task("claude-client", **credentials(first), result={"count": 3})
    next_claim = claim(reopened, selected, request="claim-next")
    assert next_claim["task"]["task_id"] == second_task["task_id"]
    assert store.complete_task("claude-client", **credentials(first), result={"count": 3}) == done
    with pytest.raises(CoordinationError, match="CLAIM_NO_LONGER_CURRENT"):
        claim(reopened, first_task)


def test_worker_capacity_serializes_distinct_tasks_across_store_connections(state):
    store, now = state
    tasks = [assignment(store, request=f"send-{i}") for i in range(2)]
    other = Store(store.path, clock=lambda: now[0])
    barrier = threading.Barrier(2)

    def attempt(candidate, task, request):
        barrier.wait(timeout=5)
        try:
            return claim(candidate, task, request=request)
        except CoordinationError as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(attempt, candidate, task, f"claim-{i}")
                   for i, (candidate, task) in enumerate(zip((store, other), tasks))]
        results = [future.result(timeout=10) for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "WORKER_AT_CAPACITY" in results
    assert sorted(store.task_status("aster-client", task_id=t["task_id"])["status"]
                  for t in tasks) == ["queued", "running"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM coord_claims").fetchone()[0] == 1
    winner = next(result for result in results if isinstance(result, dict))
    loser_index = results.index("WORKER_AT_CAPACITY")
    store.complete_task("claude-client", **credentials(winner), result={"count": 3})
    assert claim(other, tasks[loser_index], request=f"claim-{loser_index}")["task"]["status"] == "running"


def test_worker_capacity_does_not_consume_empty_claim_or_override_prior_empty_replay(state):
    store, _ = state
    assert claim(store, request="empty-before") == {"task": None}
    first = claim(store, assignment(store))
    assert claim(store, request="empty-before") == {"task": None}
    with pytest.raises(CoordinationError, match="WORKER_AT_CAPACITY"):
        claim(store, request="empty-after")
    second = assignment(store, request="send-2")
    store.complete_task("claude-client", **credentials(first), result={"count": 3})
    assert claim(store, request="empty-after")["task"]["task_id"] == second["task_id"]


def test_worker_capacity_preserves_inaccessible_task_error(state):
    store, _ = state
    claim(store, assignment(store))
    store.grant(sender="aster", receiver="outsider")
    other_task = store.delegate_task("aster-client", to_agent="outsider", request_id="send-other",
                                     instruction="Private work", expected_result="Reply")
    with pytest.raises(CoordinationError, match="TASK_NOT_ACCESSIBLE"):
        claim(store, other_task, request="wrong-receiver")


def test_worker_capacity_is_independent_per_receiver(state):
    store, _ = state
    first = claim(store, assignment(store))
    store.grant(sender="aster", receiver="outsider")
    second = store.delegate_task("aster-client", to_agent="outsider", request_id="send-other",
                                 instruction="Count entries.", expected_result="count=3")
    other = store.claim_task("outsider-client", task_id=second["task_id"], request_id="claim-1")
    assert first["task"]["status"] == other["task"]["status"] == "running"
    assert claim(store, first["task"]) == first


def test_worker_capacity_releases_on_expiry_without_reviving_original_claim(state):
    store, now = state
    first_task = assignment(store)
    second_task = assignment(store, request="send-2")
    first = claim(store, first_task, lease_seconds=15)
    with pytest.raises(CoordinationError, match="WORKER_AT_CAPACITY"):
        claim(store, second_task, request="claim-next")
    now[0] += 15
    assert claim(store, second_task, request="claim-next")["task"]["status"] == "running"
    assert store.task_status("aster-client", task_id=first_task["task_id"])["status"] == "uncertain"
    with pytest.raises(CoordinationError, match="CLAIM_NO_LONGER_CURRENT"):
        claim(store, first_task, lease_seconds=15)
    with pytest.raises(CoordinationError, match="STALE_LEASE"):
        store.complete_task("claude-client", **credentials(first), result={"count": 3})


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


def test_outcomes_are_sender_scoped_and_never_expose_coordination_secrets(state):
    store, _ = state
    own = assignment(store)
    own_claim = claim(store, own)
    store.complete_task("claude-client", **credentials(own_claim), result={"summary": "sender's result"})
    store.grant(sender="outsider", receiver="claude")
    other = store.delegate_task("outsider-client", to_agent="claude", request_id="other-sender",
                                instruction="Other work", expected_result="Other result")
    other_claim = claim(store, other, request="other-claim")
    store.complete_task("claude-client", **credentials(other_claim), result={"summary": "other private result"})
    own_page = store.outcomes("aster-client")
    assert [row["task_id"] for row in own_page["outcomes"]] == [own["task_id"]]
    assert [row["task_id"] for row in store.outcomes("outsider-client")["outcomes"]] == [other["task_id"]]
    # Being the receiver grants task-status access, not access to sender outcomes.
    assert store.outcomes("claude-client") == {"outcomes": [], "next_cursor": 0, "has_more": False}
    encoded = json.dumps(own_page)
    assert own_claim["lease_token"] not in encoded and other_claim["lease_token"] not in encoded
    assert "client_id" not in encoded and "aster-client" not in encoded and "other private result" not in encoded
    with pytest.raises(CoordinationError, match="AGENT_NOT_ENROLLED"):
        store.outcomes("unknown-client", cursor=2**63 - 1)
    store.disable_agent("aster")
    with pytest.raises(CoordinationError, match="AGENT_DISABLED"):
        store.outcomes("aster-client", cursor=2**63 - 1)


def test_outcomes_read_does_not_mutate_expiry_acceptance_or_require_native_readiness(state):
    store, now = state
    completed = assignment(store)
    store.complete_task("claude-client", **credentials(claim(store, completed)), result={"count": 3})
    assignment(store, request="overdue-queued", ttl_seconds=60)
    running = assignment(store, request="overdue-running")
    claim(store, running, request="short-lease", lease_seconds=15)
    store.register_agent(agent_id="aster", client_id="aster-client", display_name="Aster", provider="openai",
                         receive_mode="native", replace=True)
    assert store.profile("aster-client")["receive_ready"] is False
    now[0] += 61

    def snapshot():
        with sqlite3.connect(store.path) as db:
            return (db.execute("SELECT task_id,status,document FROM coord_tasks ORDER BY task_id").fetchall(),
                    db.execute("SELECT * FROM coord_events ORDER BY sequence").fetchall())

    before = snapshot()
    page = store.outcomes("aster-client")
    assert [row["task_id"] for row in page["outcomes"]] == [completed["task_id"]]
    assert page["outcomes"][0]["acceptance"] == "pending"
    assert page["outcomes"][0]["result_verification"] == "worker_reported"
    assert snapshot() == before  # No implicit expiry, read acknowledgement, or verification.


def test_outcomes_include_all_persisted_result_and_attention_states(state):
    store, now = state
    for status in ("completed", "failed", "blocked", "uncertain"):
        task = assignment(store, request="status-" + status)
        owned = claim(store, task, request="claim-" + status)
        store.complete_task("claude-client", **credentials(owned), outcome=status, result={"fixture_status": status})
    cancelled = assignment(store, request="status-cancelled")
    store.cancel_task("aster-client", task_id=cancelled["task_id"], reason="No longer needed")
    expired = assignment(store, request="status-expired", ttl_seconds=60)
    now[0] += 60
    assert store.task_status("aster-client", task_id=expired["task_id"])["status"] == "expired"
    rows = store.outcomes("aster-client")["outcomes"]
    assert [row["status"] for row in rows] == ["completed", "failed", "blocked", "uncertain", "cancelled", "expired"]


def test_outcomes_resume_after_restart_and_include_already_accepted_results(state):
    store, now = state
    task_ids = []
    for index in range(5):
        task = assignment(store, request=f"outcome-{index}", parent_task_id=task_ids[0] if task_ids else None)
        done = store.complete_task("claude-client", **credentials(claim(store, task, request=f"outcome-claim-{index}")),
                                   result={"index": index}, evidence={"fixture": True})
        if index == 1:
            store.verify_result("aster-client", task_id=task["task_id"], expected_revision=done["revision"],
                                accepted=True, evidence={"checks": ["known index"], "reference": "test:accepted"})
        task_ids.append(task["task_id"])
    cursor, recovered = 0, []
    for count, more in ((2, True), (2, True), (1, False)):
        reopened = Store(store.path, clock=lambda: now[0])
        page = reopened.outcomes("aster-client", cursor=cursor, limit=2)
        assert len(page["outcomes"]) == count and page["has_more"] is more
        assert all(row["sequence"] > cursor for row in page["outcomes"])
        assert page["next_cursor"] == page["outcomes"][-1]["sequence"]
        recovered.extend(page["outcomes"])
        cursor = page["next_cursor"]
    assert [row["task_id"] for row in recovered] == task_ids
    assert recovered[1]["acceptance"] == "accepted" and recovered[1]["result_verification"] == "sender_verified"
    assert recovered[1]["verification_evidence"]["reference"] == "test:accepted"
    assert recovered[0]["parent_task_id"] is None and recovered[2]["parent_task_id"] == task_ids[0]
    assert reopened.outcomes("aster-client", cursor=cursor) == {"outcomes": [], "next_cursor": cursor, "has_more": False}


def test_outcome_identity_survives_verification_but_new_sequence_is_recoverable(state):
    store, _ = state
    task = assignment(store)
    done = store.complete_task("claude-client", **credentials(claim(store, task)), result={"ok": True}, evidence={"count": 1})
    first = store.outcomes("aster-client")["outcomes"][0]
    verified = store.verify_result("aster-client", task_id=task["task_id"], expected_revision=done["revision"],
                                   accepted=True, evidence={"checks": ["known fixture"], "reference": "test:verified"})
    later = store.outcomes("aster-client", cursor=first["sequence"])["outcomes"][0]
    assert later["outcome_id"] == first["outcome_id"]
    assert later["task_revision"] == verified["revision"] > first["task_revision"]
    assert later["sequence"] > first["sequence"] and later["acceptance"] == "accepted"
    assert len(store.outcomes("aster-client")["outcomes"]) == 1  # Current outcomes, not all historical events.
    normalized = dict(done, status_reason=None, reconciliation_evidence=None)
    assert Store._outcome_view(normalized, 999)["outcome_id"] == first["outcome_id"]


@pytest.mark.parametrize("change", [
    {"task_id": "another-task"}, {"fence": 2}, {"status": "failed"},
    {"result": {"ok": 1}}, {"result": {"ok": 1.0}}, {"evidence": {"count": True}},
    {"status_reason": "Changed reason"}, {"reconciliation_evidence": {"reference": "operator:review"}},
])
def test_outcome_identity_changes_for_semantic_content_and_exact_json_types(state, change):
    store, _ = state
    done = store.complete_task("claude-client", **credentials(claim(store, assignment(store))),
                               result={"ok": True}, evidence={"count": 1})
    first = store.outcomes("aster-client")["outcomes"][0]
    assert Store._outcome_view({**done, **change}, first["sequence"])["outcome_id"] != first["outcome_id"]


def test_outcomes_follow_reconciliation_and_new_attempt_without_listing_obsolete_state(state):
    store, _ = state
    task = assignment(store)
    first = store.complete_task("claude-client", **credentials(claim(store, task)), outcome="uncertain", result={"observed": None})
    old_id = store.outcomes("aster-client")["outcomes"][0]["outcome_id"]
    store.reconcile_task("aster-client", task_id=task["task_id"], expected_revision=first["revision"],
                         decision="retry", evidence={"effects_absent": True, "reference": "test:fixture-had-no-effects"})
    assert store.outcomes("aster-client")["outcomes"] == []
    second = claim(store, task, request="explicit-second-attempt")
    store.complete_task("claude-client", **credentials(second), outcome="blocked", result={"observed": None})
    outcome = store.outcomes("aster-client")["outcomes"][0]
    assert outcome["fence"] == 2 and outcome["status"] == "blocked" and outcome["outcome_id"] != old_id
    assert outcome["reconciliation_evidence"]["reference"] == "test:fixture-had-no-effects"


def test_outcomes_byte_budget_keeps_full_maximal_row_and_never_skips_page_tail(state):
    store, _ = state
    def sized_payload(size, **content):
        payload = {**content, "padding": ""}
        overhead = len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        payload["padding"] = "x" * (size - overhead)
        return payload

    first = assignment(store)
    result, evidence = sized_payload(262144), sized_payload(65536)
    store.complete_task("claude-client", **credentials(claim(store, first)), outcome="uncertain", result=result, evidence=evidence)
    stopped = store.cancel_task("aster-client", task_id=first["task_id"], reason="\x01" * 4096)
    resolved = store.reconcile_task("aster-client", task_id=first["task_id"], expected_revision=stopped["revision"],
                                    decision="complete", evidence=sized_payload(65536))
    store.verify_result("aster-client", task_id=first["task_id"], expected_revision=resolved["revision"], accepted=True,
                        evidence=sized_payload(65536, checks=["fixture bounds"], reference="test:maximal-outcome"))
    task_ids = [first["task_id"]]
    for index, size in enumerate((200000, 100)):
        task = assignment(store, request=f"byte-tail-{index}")
        store.complete_task("claude-client", **credentials(claim(store, task, request=f"byte-tail-claim-{index}")),
                            result=sized_payload(size))
        task_ids.append(task["task_id"])
    first_page = store.outcomes("aster-client", limit=100)
    assert first_page["has_more"] is True and len(first_page["outcomes"]) == 1
    assert first_page["outcomes"][0]["result"] == result and first_page["outcomes"][0]["evidence"] == evidence
    assert first_page["outcomes"][0]["status_reason"] == "\x01" * 4096
    second_page = store.outcomes("aster-client", cursor=first_page["next_cursor"], limit=100)
    assert second_page["has_more"] is False and len(second_page["outcomes"]) == 2
    assert [row["task_id"] for page in (first_page, second_page) for row in page["outcomes"]] == task_ids
    for page in (first_page, second_page):
        assert len(json.dumps(page, sort_keys=True, separators=(",", ":")).encode()) <= MAX_OUTCOMES_BYTES
        assert page["next_cursor"] == page["outcomes"][-1]["sequence"]


@pytest.mark.parametrize("arguments", [{"cursor": -1}, {"cursor": True}, {"cursor": 1.0},
                                      {"limit": 0}, {"limit": 101}, {"limit": False}])
def test_outcomes_reject_invalid_pagination(state, arguments):
    store, _ = state
    with pytest.raises(CoordinationError, match="INVALID_INPUT"):
        store.outcomes("aster-client", **arguments)


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
