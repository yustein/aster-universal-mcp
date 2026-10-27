"""Bounded, read-only views over the Coordinator's existing authorization model.

Authentication and operator approval belong to the MCP entrypoint. These
functions additionally require the exact active client enrollment and retain
the Store's sender/receiver task visibility. They never invoke maintenance:
saved state, including an elapsed lease, is reported without changing it.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import sqlite3

from .store import Store, _integer


SCHEMA_VERSION = "aster.panel.v1"
TASK_SCHEMA_VERSION = "aster.panel.task.v1"
MAX_AGENTS = 100
PREVIEW_CHARACTERS = 1024
TASK_STATES = ("queued", "running", "completed", "failed", "blocked",
               "uncertain", "cancelled", "expired")
OUTCOME_STATES = frozenset({"completed", "failed", "blocked", "uncertain",
                            "cancelled", "expired"})
RECEIPT_FIELDS = ("outcome_id", "sequence", "task_revision", "status",
                  "result_verification", "acceptance")
AGENT_FIELDS = (
    "agent_id", "display_name", "provider", "receive_mode", "capabilities",
    "active", "generation", "receive_ready", "native_binding_status",
)
TASK_PREVIEW_FIELDS = (
    "task_id", "parent_task_id", "sender", "receiver", "status", "instruction",
    "expected_result", "created_at", "updated_at", "deadline", "revision",
    "fence", "lease_expires_at", "status_reason", "cancel_requested",
    "result_verification", "acceptance",
)
TASK_DETAIL_FIELDS = TASK_PREVIEW_FIELDS + (
    "contract", "authorized_scope", "checkpoint", "result", "evidence",
    "verification_evidence", "reconciliation_evidence", "delegation_depth",
)
TIME_FIELDS = ("created_at", "updated_at", "deadline", "lease_expires_at")


@contextmanager
def _read(store: Store):
    # A distinct read-only connection is intentional: Store's ordinary task
    # reads may expire leases, and its normal connection permits writes.
    with closing(sqlite3.connect(
        store.path.as_uri() + "?mode=ro", uri=True, timeout=15,
        isolation_level=None,
    )) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db


def _iso(timestamp):
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _agent_view(store, db, agent):
    public = store._view_agent(db, agent)
    # An explicit projection protects future Store fields as well as today's
    # client IDs and native binding/session credentials.
    return {key: public[key] for key in AGENT_FIELDS if key in public}


def _task_view(task, *, preview=False):
    fields = TASK_PREVIEW_FIELDS if preview else TASK_DETAIL_FIELDS
    result = {key: task[key] for key in fields if key in task}
    for key in TIME_FIELDS:
        if key in result:
            result[key] = _iso(result[key])
    if preview:
        shortened = []
        for key in ("instruction", "expected_result", "status_reason"):
            value = result.get(key)
            if isinstance(value, str) and len(value) > PREVIEW_CHARACTERS:
                result[key] = value[:PREVIEW_CHARACTERS - 1] + "…"
                shortened.append(key)
        if shortened:
            result["preview_truncated_fields"] = shortened
    return result


def snapshot(store: Store, client_id: str, limit: int = 40) -> dict:
    """Return visible totals, recent bounded task previews and permitted agents.

    Totals cover every visible assignment, not just the loaded previews. Agent
    visibility is precisely the Store.list_agents rule: self plus the receivers
    to whom the operator has granted delegation. Readiness is not a heartbeat.
    """
    _integer(limit, "limit", 1, 100)
    generated_at = _iso(store._now())
    with _read(store) as db:
        caller = store._agent(db, client_id=client_id)
        agent_id = caller["agent_id"]
        counts = {status: 0 for status in TASK_STATES}
        counts.update(total=0, accepted=0)
        for row in db.execute("""SELECT status, count(*) AS total,
                sum(CASE WHEN json_extract(document, '$.acceptance')='accepted'
                    THEN 1 ELSE 0 END) AS accepted
                FROM coord_tasks WHERE sender=? OR receiver=? GROUP BY status""",
                              (agent_id, agent_id)):
            counts[row["status"]] = row["total"]
            counts["total"] += row["total"]
            counts["accepted"] += row["accepted"]
        rows = db.execute("""SELECT document FROM coord_tasks
                WHERE sender=? OR receiver=?
                ORDER BY json_extract(document, '$.updated_at') DESC,
                         created_at DESC, task_id ASC LIMIT ?""",
                          (agent_id, agent_id, limit))
        tasks = [_task_view(json.loads(row["document"]), preview=True) for row in rows]
        agent_rows = db.execute("""SELECT document FROM coord_agents
                WHERE agent_id=? OR agent_id IN
                    (SELECT receiver FROM coord_grants WHERE sender=?)
                ORDER BY (agent_id=?) DESC, agent_id ASC LIMIT ?""",
                                (agent_id, agent_id, agent_id, MAX_AGENTS + 1)).fetchall()
        agents = [_agent_view(store, db, json.loads(row["document"]))
                  for row in agent_rows[:MAX_AGENTS]]
        viewer = _agent_view(store, db, caller)

    has_more = counts["total"] > len(tasks)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "viewer": viewer,
        "counts": counts,
        "tasks": tasks,
        "agents": agents,
        "has_more": has_more,
        "agents_truncated": len(agent_rows) > MAX_AGENTS,
        "sources": {
            "coordination": {
                "status": "available", "label": "Coordinator",
                "checked_at": generated_at, "tasks_returned": len(tasks),
                "visible_tasks": counts["total"], "task_limit": limit,
                "message": "Saved task state and authorized agent readiness. "
                           "Lease expiry is not applied by this view. "
                           "Counts cover all visible tasks; the list shows the most recently updated.",
            },
            "archive": {
                "status": "not_reported", "label": "Archive",
                "message": "No archive measurement has been published to this view.",
            },
            "costs": {
                "status": "not_reported", "label": "Costs",
                "message": "No cost ledger measurement has been published to this view.",
            },
        },
    }


def task_detail(store: Store, client_id: str, task_id: str) -> dict:
    """Read one saved task under the existing visibility rule, without expiry.

    Evidence and result bodies are the existing bounded task documents. Only
    the sender can see its canonical persisted outcome record, matching the
    Store.outcomes feed. That record is not proof of delivery or acceptance.
    """
    generated_at = _iso(store._now())
    with _read(store) as db:
        caller, task = store._visible(db, client_id, task_id)
        result = _task_view(task)
        if caller["agent_id"] == task["sender"] and task["status"] in OUTCOME_STATES:
            # Match the outcome feed's latest event addressed to this sender.
            # The event and current task are read in the same transaction; no
            # feed cursor, receipt, lease or task state is changed here.
            sequence = db.execute("""SELECT max(sequence) FROM coord_events
                    WHERE task_id=? AND recipient=?""",
                                  (task["task_id"], caller["agent_id"])).fetchone()[0]
            if sequence is not None:
                outcome = store._outcome_view(task, sequence)
                result["receipt"] = {
                    "kind": "coordinator_outcome_record", "source": "Coordinator",
                    **{key: outcome[key] for key in RECEIPT_FIELDS if key in outcome},
                }
    return {"schema_version": TASK_SCHEMA_VERSION,
            "generated_at": generated_at, "task": result}
