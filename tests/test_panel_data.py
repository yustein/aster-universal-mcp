"""Panel projections preserve authorization and do not advance task state."""
from __future__ import annotations

import json
import sqlite3

import pytest

from coordination import panel_data
from coordination.store import CoordinationError, Store


@pytest.fixture
def state(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / "coordination.sqlite", clock=lambda: now[0])
    for name in ("sender", "worker", "other", "other_worker"):
        store.register_agent(agent_id=name, client_id="private-client-" + name,
                             display_name=name.title(), provider="fixture")
    store.grant(sender="sender", receiver="worker")
    store.grant(sender="other", receiver="other_worker")
    return store, now


def assign(store, request="one", *, sender="sender", receiver="worker", **kwargs):
    return store.delegate_task("private-client-" + sender, to_agent=receiver,
                               request_id=request, instruction="Visible assignment",
                               expected_result="An evidence-backed answer", **kwargs)


def dump(store):
    with sqlite3.connect(store.path) as db:
        return tuple(db.iterdump())


def test_snapshot_counts_all_visible_tasks_while_bounding_recent_previews(state):
    store, now = state
    first = assign(store)
    now[0] += 1
    second = assign(store, "two")
    foreign = assign(store, "foreign", sender="other", receiver="other_worker")
    data = panel_data.snapshot(store, "private-client-sender", limit=1)
    assert data["schema_version"] == "aster.panel.v1"
    assert data["counts"]["total"] == data["counts"]["queued"] == 2
    assert data["counts"]["accepted"] == 0
    assert [item["task_id"] for item in data["tasks"]] == [second["task_id"]]
    assert data["has_more"] is True
    assert first["task_id"] not in json.dumps(data)
    assert foreign["task_id"] not in json.dumps(data)
    assert data["generated_at"] == "1970-01-01T00:16:41Z"
    assert data["tasks"][0]["created_at"] == data["generated_at"]
    assert data["sources"]["costs"]["status"] == "not_reported"
    assert "reported_spend_usd" not in data["sources"]["costs"]


def test_sender_receiver_visibility_and_exact_fleet_grants(state):
    store, _ = state
    task = assign(store)
    for name in ("sender", "worker"):
        data = panel_data.snapshot(store, "private-client-" + name)
        assert data["viewer"]["agent_id"] == name
        assert data["tasks"][0]["task_id"] == task["task_id"]
        assert panel_data.task_detail(store, "private-client-" + name, task["task_id"])["task"]["task_id"] == task["task_id"]
    assert {a["agent_id"] for a in panel_data.snapshot(store, "private-client-sender")["agents"]} == {"sender", "worker"}
    assert {a["agent_id"] for a in panel_data.snapshot(store, "private-client-worker")["agents"]} == {"worker"}
    assert panel_data.snapshot(store, "private-client-other")["tasks"] == []
    errors = []
    for task_id in (task["task_id"], "task_absent"):
        with pytest.raises(CoordinationError) as error:
            panel_data.task_detail(store, "private-client-other", task_id)
        assert error.value.code == "TASK_NOT_ACCESSIBLE"
        errors.append(str(error.value))
    assert errors[0] == errors[1]


@pytest.mark.parametrize("read", [
    lambda store, client, task: panel_data.snapshot(store, client),
    lambda store, client, task: panel_data.task_detail(store, client, task),
])
def test_exact_active_enrollment_is_required(state, read):
    store, _ = state
    task = assign(store)
    with pytest.raises(CoordinationError, match="AGENT_NOT_ENROLLED"):
        read(store, "sender", task["task_id"])
    store.disable_agent("sender")
    with pytest.raises(CoordinationError, match="AGENT_DISABLED"):
        read(store, "private-client-sender", task["task_id"])


@pytest.mark.parametrize("limit", [True, False, "10", None, 0, -1, 101, 1.0])
def test_limits_are_strict_bounded_integers(state, limit):
    with pytest.raises(CoordinationError, match="INVALID_INPUT"):
        panel_data.snapshot(state[0], "private-client-sender", limit=limit)


def test_reads_do_not_mutate_elapsed_leases_events_or_receipts(state, monkeypatch):
    store, now = state
    task = assign(store)
    lease = store.claim_task("private-client-worker", request_id="claim", task_id=task["task_id"])
    now[0] += 301
    before = dump(store)
    def forbidden(*args, **kwargs):
        raise AssertionError("Panel called a maintenance/write path")
    monkeypatch.setattr(store, "_maintain", forbidden)
    monkeypatch.setattr(store, "_connect", forbidden)
    snapshot = panel_data.snapshot(store, "private-client-sender")
    detail = panel_data.task_detail(store, "private-client-sender", task["task_id"])
    assert snapshot["counts"]["running"] == 1
    assert detail["task"]["status"] == "running"
    assert lease["lease_token"] not in json.dumps((snapshot, detail))
    assert dump(store) == before
    with panel_data._read(store) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM coord_tasks")


def test_saved_completion_and_acceptance_are_distinct_and_private_fields_are_absent(state):
    store, _ = state
    task = assign(store, authorized_scope={"changes": "None"})
    claim = store.claim_task("private-client-worker", request_id="claim", task_id=task["task_id"])
    done = store.complete_task("private-client-worker", task_id=task["task_id"],
                              lease_token=claim["lease_token"], fence=claim["fence"],
                              result={"summary": "Observed result"}, evidence={"reference": "fixture:source"})
    detail = panel_data.task_detail(store, "private-client-sender", task["task_id"])["task"]
    assert detail["result"] == {"summary": "Observed result"}
    assert detail["evidence"] == {"reference": "fixture:source"}
    assert detail["status"] == "completed"
    assert detail["result_verification"] == "worker_reported"
    assert detail["acceptance"] == "pending"
    pending_receipt = detail["receipt"]
    assert pending_receipt["acceptance"] == "pending"
    assert pending_receipt["result_verification"] == "worker_reported"
    store.verify_result("private-client-sender", task_id=task["task_id"],
                        expected_revision=done["revision"], accepted=True,
                        evidence={"reference": "fixture:review", "checks": ["Compared source"]})
    detail = panel_data.task_detail(store, "private-client-sender", task["task_id"])["task"]
    assert detail["acceptance"] == "accepted"
    assert detail["result_verification"] == "sender_verified"
    assert detail["verification_evidence"]["reference"] == "fixture:review"
    accepted_receipt = detail["receipt"]
    assert accepted_receipt["acceptance"] == "accepted"
    assert accepted_receipt["result_verification"] == "sender_verified"
    assert accepted_receipt["outcome_id"] == pending_receipt["outcome_id"]
    assert accepted_receipt["sequence"] > pending_receipt["sequence"]
    assert accepted_receipt["task_revision"] > pending_receipt["task_revision"]
    expected = store.outcomes("private-client-sender")["outcomes"][0]
    for field in panel_data.RECEIPT_FIELDS:
        assert accepted_receipt[field] == expected[field]
    assert panel_data.snapshot(store, "private-client-sender")["counts"]["accepted"] == 1
    serialized = json.dumps(detail)
    assert claim["lease_token"] not in serialized
    assert "private-client-" not in serialized
    assert not any(key.startswith("_") for key in detail)


@pytest.mark.parametrize("outcome", ["completed", "failed", "blocked", "uncertain",
                                    "cancelled", "expired"])
def test_sender_receipt_matches_canonical_outcome_without_expanding_receiver_access_or_writing(state, monkeypatch, outcome):
    store, now = state
    task = assign(store)
    if outcome == "cancelled":
        store.cancel_task("private-client-sender", task_id=task["task_id"], reason="Fixture cancellation")
    elif outcome == "expired":
        now[0] += 86401
        store.task_status("private-client-sender", task_id=task["task_id"])
    else:
        claim = store.claim_task("private-client-worker", request_id="claim", task_id=task["task_id"])
        store.complete_task("private-client-worker", task_id=task["task_id"],
                            lease_token=claim["lease_token"], fence=claim["fence"],
                            outcome=outcome, result={"summary": "Saved outcome"})
    expected = store.outcomes("private-client-sender")["outcomes"][0]
    before = dump(store)
    def forbidden(*args, **kwargs):
        raise AssertionError("Panel must use its read-only transaction, not maintenance or outcomes()")
    for method in ("_maintain", "_connect", "outcomes"):
        monkeypatch.setattr(store, method, forbidden)
    sender = panel_data.task_detail(store, "private-client-sender", task["task_id"])["task"]
    assert sender["receipt"] == {
        "kind": "coordinator_outcome_record", "source": "Coordinator",
        **{field: expected[field] for field in panel_data.RECEIPT_FIELDS},
    }
    receiver = panel_data.task_detail(store, "private-client-worker", task["task_id"])["task"]
    assert receiver["status"] == outcome
    assert "receipt" not in receiver
    assert expected["outcome_id"] not in json.dumps(receiver)
    assert dump(store) == before


def test_live_assignments_have_no_outcome_receipt(state):
    store, _ = state
    task = assign(store)
    assert "receipt" not in panel_data.task_detail(store, "private-client-sender", task["task_id"])["task"]
    store.claim_task("private-client-worker", request_id="claim", task_id=task["task_id"])
    assert "receipt" not in panel_data.task_detail(store, "private-client-sender", task["task_id"])["task"]


def test_agent_readiness_does_not_expose_native_binding_identity(state):
    store, now = state
    store.register_agent(agent_id="native", client_id="private-client-native",
                         display_name="Native", provider="fixture", receive_mode="native")
    store.grant(sender="sender", receiver="native")
    store.bind_receiver(agent_id="native", subscription_id="private-subscription",
                        expected_session_id="private-session", expires_at=now[0] + 60)
    store.verify_receiver(agent_id="native", subscription_id="private-subscription",
                          observed_session_id="private-session", evidence_ref="private-binding-evidence")
    snapshot = panel_data.snapshot(store, "private-client-sender")
    native = next(agent for agent in snapshot["agents"] if agent["agent_id"] == "native")
    assert native["receive_ready"] is True
    assert native["native_binding_status"] == "operator_verified"
    serialized = json.dumps(snapshot)
    for value in ("private-client", "private-subscription", "private-session", "private-binding-evidence"):
        assert value not in serialized
    now[0] += 61
    before = dump(store)
    native = next(agent for agent in panel_data.snapshot(store, "private-client-sender")["agents"] if agent["agent_id"] == "native")
    assert native["receive_ready"] is False
    assert native["native_binding_status"] == "pending_or_expired"
    assert dump(store) == before


def test_overview_is_bounded_but_detail_keeps_saved_evidence_and_full_instruction(state):
    store, _ = state
    instruction = "Long but authorized task text. " * 1000
    task = store.delegate_task("private-client-sender", to_agent="worker", request_id="long",
                               instruction=instruction, expected_result="Read full task detail")
    snapshot = panel_data.snapshot(store, "private-client-sender")
    assert len(snapshot["tasks"][0]["instruction"]) == panel_data.PREVIEW_CHARACTERS
    assert snapshot["tasks"][0]["preview_truncated_fields"] == ["instruction"]
    assert panel_data.task_detail(store, "private-client-sender", task["task_id"])["task"]["instruction"] == instruction
