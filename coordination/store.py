"""Transactional assignments, exclusive leases and evidence-aware receipts.

This is a separate store, never the PA production database. The PA work contract
supplies stable work IDs, bounded authority, generations and checkpoints; the
reviewed binding work supplies the rule that declared session IDs are not proof.
SQLite serializes claims across processes. Lease loss requires reconciliation,
because fencing a receipt cannot undo an external side effect.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Callable

from . import CONTRACT_VERSION


class CoordinationError(ValueError):
    """Expected, safe-to-display contract failure; never includes secret values."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _error(code: str, message: str):
    raise CoordinationError(code, message)


def _text(value, field: str, maximum=65536):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _error("INVALID_INPUT", f"{field} must be nonempty text without NUL characters.")
    if len(value.encode("utf-8")) > maximum:
        _error("INVALID_INPUT", f"{field} exceeds its size limit.")
    return value


def _id(value, field="identifier"):
    _text(value, field, 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        _error("INVALID_INPUT", f"{field} contains unsupported characters.")
    return value


def _integer(value, field, low, high):
    if type(value) is not int or not low <= value <= high:
        _error("INVALID_INPUT", f"{field} is outside its integer range.")
    return value


def _json(value, field, maximum=65536, *, optional=False):
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        _error("INVALID_INPUT", f"{field} must be a JSON object.")
    # JSON keys must already be strings; silently changing keys breaks hashes.
    def validate(item, depth=0):
        if depth > 32:
            _error("INVALID_INPUT", f"{field} is nested too deeply.")
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                _error("INVALID_INPUT", f"{field} contains a non-string key.")
            for child in item.values():
                validate(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            _error("INVALID_INPUT", f"{field} contains a non-JSON value.")
    validate(value)
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        _error("INVALID_INPUT", f"{field} is not finite JSON.")
    if len(raw.encode()) > maximum:
        _error("INVALID_INPUT", f"{field} exceeds its size limit.")
    return json.loads(raw)


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_dump(value).encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS coord_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coord_agents (
 agent_id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coord_grants (
 sender TEXT NOT NULL, receiver TEXT NOT NULL, PRIMARY KEY(sender,receiver));
CREATE TABLE IF NOT EXISTS coord_bindings (
 agent_id TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coord_tasks (
 task_id TEXT PRIMARY KEY, sender TEXT NOT NULL, receiver TEXT NOT NULL,
 request_id TEXT NOT NULL, request_hash TEXT NOT NULL, status TEXT NOT NULL,
 created_at REAL NOT NULL, document TEXT NOT NULL, UNIQUE(sender,request_id));
CREATE INDEX IF NOT EXISTS coord_task_inbox ON coord_tasks(receiver,status,created_at);
CREATE TABLE IF NOT EXISTS coord_claims (
 receiver TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 task_id TEXT, PRIMARY KEY(receiver,request_id));
CREATE TABLE IF NOT EXISTS coord_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 recipient TEXT NOT NULL, event_type TEXT NOT NULL, task_id TEXT NOT NULL,
 payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS coord_event_recipient ON coord_events(recipient,sequence);
"""


class Store:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        if not self.path.is_absolute():
            _error("INVALID_CONFIGURATION", "Coordination database must use an absolute path.")
        if self.path.is_symlink():
            _error("INVALID_CONFIGURATION", "Coordination database must not be a symlink.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # O_EXCL gives newly-created database files private permissions before
        # SQLite opens them. Existing protected state is not chmod'ed silently.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        if os.name != "nt" and self.path.stat().st_mode & 0o077:
            _error("INVALID_CONFIGURATION", "Coordination database must be private to its operator.")
        self.clock = clock
        with closing(self._connect()) as db:
            existing = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if any(not name.startswith(("coord_", "sqlite_")) for name in existing):
                _error("STATE_NAMESPACE_CONFLICT", "Use a separate coordination database; existing application state is not a migration target.")
            if "coord_meta" in existing:
                version = db.execute("SELECT value FROM coord_meta WHERE key='schema'").fetchone()
                if version is not None and version[0] != "1":
                    _error("INVALID_CONFIGURATION", "Unsupported coordination database schema.")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            version = db.execute("SELECT value FROM coord_meta WHERE key='schema'").fetchone()
            if version is not None and version[0] != "1":
                _error("INVALID_CONFIGURATION", "Unsupported coordination database schema.")
            db.execute("INSERT OR IGNORE INTO coord_meta VALUES ('schema','1')")

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self):
        now = float(self.clock())
        if not math.isfinite(now) or now < 0:
            _error("INVALID_CONFIGURATION", "Clock is unavailable.")
        return now

    def _agent(self, db, *, client_id=None, agent_id=None, active=True):
        if client_id is not None:
            _text(client_id, "authenticated client", 512)
            row = db.execute("SELECT document FROM coord_agents WHERE client_id=?", (client_id,)).fetchone()
        else:
            _id(agent_id, "agent_id")
            row = db.execute("SELECT document FROM coord_agents WHERE agent_id=?", (agent_id,)).fetchone()
        if row is None:
            _error("AGENT_NOT_ENROLLED", "The local operator must enroll this exact authenticated client.")
        result = json.loads(row[0])
        if active and not result["active"]:
            _error("AGENT_DISABLED", "This agent's coordination access is disabled.")
        return result

    @staticmethod
    def _public_agent(agent):
        return {key: value for key, value in agent.items() if key != "client_id"}

    def _binding(self, db, agent_id):
        row = db.execute("SELECT document FROM coord_bindings WHERE agent_id=?", (agent_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _ready(self, db, agent):
        if not agent["active"]:
            return False
        if agent["receive_mode"] == "pull":
            return True
        binding = self._binding(db, agent["agent_id"])
        return bool(binding and binding["active"] and binding["operator_verified"]
                    and binding["generation"] == agent["generation"]
                    and binding["expires_at"] > self._now())

    def _view_agent(self, db, agent):
        result = self._public_agent(agent)
        result["receive_ready"] = self._ready(db, agent)
        binding = self._binding(db, agent["agent_id"])
        result["native_binding_status"] = (
            "operator_verified" if result["receive_ready"] and agent["receive_mode"] == "native"
            else "pending_or_expired" if binding else "not_configured")
        return result

    def _put_agent(self, db, agent):
        db.execute("INSERT INTO coord_agents VALUES (?,?,?) ON CONFLICT(agent_id) DO UPDATE SET client_id=excluded.client_id, document=excluded.document",
                   (agent["agent_id"], agent["client_id"], _dump(agent)))

    def _save(self, db, task):
        db.execute("UPDATE coord_tasks SET status=?, document=? WHERE task_id=?",
                   (task["status"], _dump(task), task["task_id"]))

    def _event(self, db, recipient, event_type, task):
        payload = {"contract": CONTRACT_VERSION, "task_id": task["task_id"],
                   "status": task["status"], "revision": task["revision"],
                   "sender": task["sender"], "receiver": task["receiver"]}
        db.execute("INSERT INTO coord_events(event_id,recipient,event_type,task_id,payload,created_at) VALUES (?,?,?,?,?,?)",
                   ("evt_" + secrets.token_hex(16), recipient, event_type, task["task_id"], _dump(payload), self._now()))

    def _changed(self, db, task, *, queued=False):
        task["revision"] += 1
        task["updated_at"] = self._now()
        self._save(db, task)
        terminal = task["status"] in ("completed", "failed", "cancelled", "expired")
        self._event(db, task["sender"], "task.completed" if terminal else "task.updated", task)
        if queued:
            self._event(db, task["receiver"], "task.queued", task)

    def _expire(self, db):
        now = self._now()
        rows = db.execute("SELECT document FROM coord_tasks WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            task = json.loads(row[0])
            if task["status"] == "queued" and task["deadline"] <= now:
                task["status"] = "expired"
                task["status_reason"] = "Assignment deadline elapsed before claim."
                self._changed(db, task)
            elif task["status"] == "running" and (
                    task["deadline"] <= now or task["lease_expires_at"] <= now
                    or not self._ready(db, self._agent(db, agent_id=task["receiver"], active=False))):
                task["status"] = "uncertain"
                task["status_reason"] = "Lease, deadline or receiver binding expired; reconcile possible external effects before retry."
                self._changed(db, task)

    def _maintain(self):
        # Commit expiration even if the following operation rejects a stale
        # worker. Otherwise raising would roll back the uncertainty record.
        with self._transaction() as db:
            self._expire(db)

    def _invalidate(self, db, agent_id, reason):
        for row in db.execute("SELECT document FROM coord_tasks WHERE receiver=? AND status='running'", (agent_id,)).fetchall():
            task = json.loads(row[0])
            task["status"] = "uncertain"
            task["status_reason"] = reason
            task["lease_expires_at"] = self._now()
            self._changed(db, task)

    def register_agent(self, *, agent_id, client_id, display_name, provider,
                       receive_mode="pull", capabilities=None, replace=False):
        """Local operator only. No MCP registration tool calls this method."""
        _id(agent_id, "agent_id")
        _text(client_id, "client_id", 512)
        _text(display_name, "display_name", 128)
        _text(provider, "provider", 64)
        if receive_mode not in ("pull", "native") or type(replace) is not bool:
            _error("INVALID_INPUT", "Unsupported receiving mode or replacement flag.")
        capabilities = [] if capabilities is None else capabilities
        if not isinstance(capabilities, list) or len(capabilities) > 32:
            _error("INVALID_INPUT", "Capabilities must be a bounded list.")
        for capability in capabilities:
            _id(capability, "capability")
        with self._transaction() as db:
            row = db.execute("SELECT document FROM coord_agents WHERE agent_id=?", (agent_id,)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous and not replace:
                _error("AGENT_EXISTS", "Explicit operator replacement is required.")
            collision = db.execute("SELECT agent_id FROM coord_agents WHERE client_id=?", (client_id,)).fetchone()
            if collision and collision[0] != agent_id:
                _error("CLIENT_ALREADY_ENROLLED", "Use a distinct OAuth client registration for each agent.")
            agent = {"agent_id": agent_id, "client_id": client_id, "display_name": display_name,
                     "provider": provider, "receive_mode": receive_mode,
                     "capabilities": sorted(set(capabilities)), "active": True,
                     "generation": previous["generation"] + 1 if previous else 1}
            self._put_agent(db, agent)
            if previous:
                self._invalidate(db, agent_id, "Receiver enrollment was replaced; old workers are fenced.")
                db.execute("DELETE FROM coord_bindings WHERE agent_id=?", (agent_id,))
            return self._view_agent(db, agent)

    def grant(self, *, sender, receiver, allowed=True):
        if type(allowed) is not bool or sender == receiver:
            _error("INVALID_INPUT", "Grant requires distinct agents and a boolean permission.")
        with self._transaction() as db:
            self._agent(db, agent_id=sender)
            self._agent(db, agent_id=receiver)
            if allowed:
                db.execute("INSERT OR IGNORE INTO coord_grants VALUES (?,?)", (sender, receiver))
            else:
                db.execute("DELETE FROM coord_grants WHERE sender=? AND receiver=?", (sender, receiver))
        return {"sender": sender, "receiver": receiver, "allowed": allowed}

    def disable_agent(self, agent_id):
        with self._transaction() as db:
            agent = self._agent(db, agent_id=agent_id, active=False)
            agent["active"] = False
            agent["generation"] += 1
            self._put_agent(db, agent)
            self._invalidate(db, agent_id, "Receiver disabled; reconcile prior effects.")
            return self._public_agent(agent)

    def agent_for_client(self, client_id):
        with closing(self._connect()) as db:
            return self._agent(db, client_id=client_id)

    def profile(self, client_id):
        with closing(self._connect()) as db:
            agent = self._agent(db, client_id=client_id)
            return {"contract": CONTRACT_VERSION, **self._view_agent(db, agent)}

    def list_agents(self, client_id):
        with closing(self._connect()) as db:
            caller = self._agent(db, client_id=client_id)
            rows = db.execute("SELECT document FROM coord_agents WHERE agent_id=? OR agent_id IN (SELECT receiver FROM coord_grants WHERE sender=?) ORDER BY agent_id",
                              (caller["agent_id"], caller["agent_id"])).fetchall()
            return {"agents": [self._view_agent(db, json.loads(row[0])) for row in rows]}

    def bind_receiver(self, *, agent_id, subscription_id, expected_session_id, expires_at):
        _id(subscription_id, "subscription_id")
        _text(expected_session_id, "expected_session_id", 256)
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at) or not self._now() < expires_at <= self._now() + 604800:
            _error("INVALID_INPUT", "Binding expiry must be within seven days.")
        with self._transaction() as db:
            agent = self._agent(db, agent_id=agent_id)
            if agent["receive_mode"] != "native":
                _error("RECEIVE_MODE_MISMATCH", "Operator must enroll this agent for native receiving.")
            previous = self._binding(db, agent_id)
            if (previous and previous["active"] and previous["expires_at"] > self._now()
                    and previous["subscription_id"] == subscription_id
                    and previous["expected_session_id"] == expected_session_id
                    and previous["generation"] == agent["generation"]):
                binding = previous
                binding["expires_at"] = expires_at
            else:
                agent["generation"] += 1
                self._put_agent(db, agent)
                self._invalidate(db, agent_id, "Native receiver binding changed; reconcile prior effects.")
                binding = {"agent_id": agent_id, "subscription_id": subscription_id,
                           "expected_session_id": expected_session_id, "observed_session_id": None,
                           "generation": agent["generation"], "expires_at": expires_at, "active": True,
                           "operator_verified": False, "evidence_source": None, "evidence_ref": None}
            db.execute("INSERT INTO coord_bindings VALUES (?,?) ON CONFLICT(agent_id) DO UPDATE SET document=excluded.document", (agent_id, _dump(binding)))
            return dict(binding)

    def verify_receiver(self, *, agent_id, subscription_id, observed_session_id,
                        evidence_ref, evidence_source="operator_observed"):
        """Trusted local operator attestation, not a model or provider assertion."""
        _text(observed_session_id, "observed_session_id", 256)
        _text(evidence_ref, "evidence_ref", 2048)
        if evidence_source != "operator_observed":
            _error("INVALID_EVIDENCE", "Only a trusted local operator observation is supported.")
        with self._transaction() as db:
            agent = self._agent(db, agent_id=agent_id)
            binding = self._binding(db, agent_id)
            if not binding or not binding["active"] or binding["subscription_id"] != subscription_id or binding["generation"] != agent["generation"] or binding["expires_at"] <= self._now():
                _error("STALE_BINDING", "No matching fresh native binding exists.")
            if observed_session_id != binding["expected_session_id"]:
                _error("WRONG_RECEIVER", "Actual observed destination does not match intended destination.")
            binding.update(operator_verified=True, observed_session_id=observed_session_id,
                           evidence_ref=evidence_ref, evidence_source=evidence_source,
                           verified_at=self._now())
            db.execute("UPDATE coord_bindings SET document=? WHERE agent_id=?", (_dump(binding), agent_id))
            return dict(binding)

    def revoke_receiver(self, agent_id, subscription_id):
        with self._transaction() as db:
            agent = self._agent(db, agent_id=agent_id, active=False)
            binding = self._binding(db, agent_id)
            if not binding or binding["subscription_id"] != subscription_id:
                _error("STALE_BINDING", "Subscription does not own this binding.")
            if binding["active"]:
                binding["active"] = False
                binding["operator_verified"] = False
                agent["generation"] += 1
                self._put_agent(db, agent)
                self._invalidate(db, agent_id, "Native subscription revoked; reconcile prior effects.")
                db.execute("UPDATE coord_bindings SET document=? WHERE agent_id=?", (_dump(binding), agent_id))
            return dict(binding)

    def native_binding(self, agent_id):
        with closing(self._connect()) as db:
            agent = self._agent(db, agent_id=agent_id, active=False)
            binding = self._binding(db, agent_id)
            if binding:
                binding["ready"] = self._ready(db, agent) and agent["receive_mode"] == "native"
            return binding

    @staticmethod
    def _task(db, task_id):
        _id(task_id, "task_id")
        row = db.execute("SELECT document FROM coord_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            _error("TASK_NOT_ACCESSIBLE", "Task is absent or not accessible to this agent.")
        return json.loads(row[0])

    @staticmethod
    def _view(task):
        return {key: value for key, value in task.items() if not key.startswith("_")}

    def _visible(self, db, client_id, task_id, *, sender_only=False):
        agent = self._agent(db, client_id=client_id)
        task = self._task(db, task_id)
        allowed = (task["sender"],) if sender_only else (task["sender"], task["receiver"])
        if agent["agent_id"] not in allowed:
            _error("TASK_NOT_ACCESSIBLE", "Task is absent or not accessible to this agent.")
        return agent, task

    def delegate_task(self, client_id, *, to_agent, request_id, instruction, expected_result,
                      inputs=None, authorized_scope=None, ttl_seconds=86400, parent_task_id=None):
        _id(to_agent, "to_agent")
        _id(request_id, "request_id")
        if parent_task_id is not None:
            _id(parent_task_id, "parent_task_id")
        _text(instruction, "instruction")
        _text(expected_result, "expected_result", 8192)
        _integer(ttl_seconds, "ttl_seconds", 60, 604800)
        inputs = _json(inputs, "inputs", optional=True)
        authorized_scope = _json(authorized_scope, "authorized_scope", 8192, optional=True)
        body = dict(to_agent=to_agent, instruction=instruction, expected_result=expected_result,
                    inputs=inputs, authorized_scope=authorized_scope, ttl_seconds=ttl_seconds,
                    parent_task_id=parent_task_id)
        digest = _hash(body)
        self._maintain()
        with self._transaction() as db:
            sender = self._agent(db, client_id=client_id)
            sid = sender["agent_id"]
            prior = db.execute("SELECT request_hash,document FROM coord_tasks WHERE sender=? AND request_id=?", (sid, request_id)).fetchone()
            if prior:
                if not hmac.compare_digest(prior[0], digest):
                    _error("IDEMPOTENCY_CONFLICT", "This request ID already names a different assignment.")
                return self._view(json.loads(prior[1]))
            permitted = db.execute("SELECT 1 FROM coord_grants WHERE sender=? AND receiver=?", (sid, to_agent)).fetchone()
            if not permitted:
                _error("ROUTE_NOT_ALLOWED", "The local operator has not granted this delegation route.")
            receiver = self._agent(db, agent_id=to_agent)
            depth = 0
            if parent_task_id is not None:
                _, parent = self._visible(db, client_id, parent_task_id)
                depth = parent.get("delegation_depth", 0) + 1
                if depth > 8:
                    _error("DELEGATION_LIMIT", "Maximum delegation depth reached.")
            if db.execute("SELECT count(*) FROM coord_tasks WHERE receiver=? AND status IN ('queued','running')", (to_agent,)).fetchone()[0] >= 1000:
                _error("QUEUE_FULL", "Receiver already has the maximum pending assignments.")
            now = self._now()
            task = {"contract": CONTRACT_VERSION, "task_id": "task_" + secrets.token_hex(16),
                    "request_id": request_id, "sender": sid, "receiver": to_agent,
                    "status": "queued", "instruction": instruction, "expected_result": expected_result,
                    "inputs": inputs, "authorized_scope": authorized_scope, "created_at": now,
                    "updated_at": now, "deadline": now + ttl_seconds, "revision": 0,
                    "fence": 0, "binding_generation": receiver["generation"], "lease_expires_at": None,
                    "checkpoint": None, "result": None, "evidence": None,
                    "cancel_requested": False, "parent_task_id": parent_task_id, "delegation_depth": depth,
                    "result_verification": "not_submitted", "acceptance": "pending", "_lease_token": None,
                    "_claim_request_id": None, "_completion_hash": None}
            db.execute("INSERT INTO coord_tasks VALUES (?,?,?,?,?,?,?,?)", (task["task_id"], sid, to_agent, request_id, digest, "queued", now, _dump(task)))
            self._event(db, to_agent, "task.queued", task)
            return self._view(task)

    def inbox(self, client_id, *, limit=20):
        _integer(limit, "limit", 1, 100)
        self._maintain()
        with closing(self._connect()) as db:
            agent = self._agent(db, client_id=client_id)
            rows = db.execute("SELECT document FROM coord_tasks WHERE receiver=? AND status='queued' ORDER BY created_at,task_id LIMIT ?", (agent["agent_id"], limit)).fetchall()
            return {"tasks": [self._view(json.loads(row[0])) for row in rows],
                    "receive_ready": self._ready(db, agent)}

    @staticmethod
    def _claim_view(task):
        return {"task": Store._view(task), "lease_token": task["_lease_token"],
                "fence": task["fence"], "lease_expires_at": task["lease_expires_at"]}

    def claim_task(self, client_id, *, request_id, task_id=None, lease_seconds=300):
        _id(request_id, "request_id")
        if task_id is not None:
            _id(task_id, "task_id")
        _integer(lease_seconds, "lease_seconds", 15, 3600)
        digest = _hash({"task_id": task_id, "lease_seconds": lease_seconds})
        self._maintain()
        with self._transaction() as db:
            self._expire(db)
            agent = self._agent(db, client_id=client_id)
            aid = agent["agent_id"]
            if not self._ready(db, agent):
                _error("RECEIVER_NOT_READY", "Native receiver needs a fresh operator-verified destination binding.")
            prior = db.execute("SELECT request_hash,task_id FROM coord_claims WHERE receiver=? AND request_id=?", (aid, request_id)).fetchone()
            if prior:
                if not hmac.compare_digest(prior[0], digest):
                    _error("IDEMPOTENCY_CONFLICT", "Claim request ID was reused with different arguments.")
                if prior[1] is None:
                    return {"task": None}
                task = self._task(db, prior[1])
                now = self._now()
                if (task["status"] != "running" or task["_claim_request_id"] != request_id
                        or task["binding_generation"] != agent["generation"]
                        or task["lease_expires_at"] <= now or task["deadline"] <= now):
                    _error("CLAIM_NO_LONGER_CURRENT", "The original claim is no longer executable; inspect its saved task.")
                return self._claim_view(task)
            if task_id is not None:
                task = self._task(db, task_id)
                if task["receiver"] != aid:
                    _error("TASK_NOT_ACCESSIBLE", "Task is absent or not accessible to this agent.")
                if task["status"] != "queued":
                    _error("TASK_NOT_QUEUED", "Task is already claimed or requires reconciliation.")
            else:
                row = db.execute("SELECT document FROM coord_tasks WHERE receiver=? AND status='queued' ORDER BY created_at,task_id LIMIT 1", (aid,)).fetchone()
                task = json.loads(row[0]) if row else None
            now = self._now()
            if task and task["deadline"] <= now:
                # Time may have advanced since the maintenance scan. Never
                # return an executable claim whose deadline is already past.
                _error("TASK_NOT_QUEUED", "Assignment deadline elapsed before claim.")
            db.execute("INSERT INTO coord_claims VALUES (?,?,?,?)", (aid, request_id, digest, task["task_id"] if task else None))
            if not task:
                return {"task": None}
            task.update(status="running", fence=task["fence"] + 1,
                        binding_generation=agent["generation"], _lease_token=secrets.token_urlsafe(32),
                        _claim_request_id=request_id, lease_expires_at=min(now + lease_seconds, task["deadline"]))
            self._changed(db, task)
            return self._claim_view(task)

    def _lease(self, db, client_id, task_id, lease_token, fence, *, terminal_retry=False):
        _text(lease_token, "lease_token", 256)
        _integer(fence, "fence", 1, 2**63 - 1)
        agent, task = self._visible(db, client_id, task_id)
        if (task["receiver"] != agent["agent_id"] or task["binding_generation"] != agent["generation"]
                or task["fence"] != fence or not task.get("_lease_token")
                or not hmac.compare_digest(task["_lease_token"], lease_token)):
            _error("STALE_LEASE", "Lease does not belong to this receiver's current claim.")
        if terminal_retry and task["_completion_hash"]:
            return task
        if not self._ready(db, agent):
            _error("RECEIVER_NOT_READY", "Receiver binding is no longer valid.")
        if task["status"] != "running" or task["lease_expires_at"] <= self._now() or task["deadline"] <= self._now():
            _error("STALE_LEASE", "Lease is no longer executable; inspect and reconcile the task.")
        return task

    def renew_lease(self, client_id, *, task_id, lease_token, fence, lease_seconds=300):
        _integer(lease_seconds, "lease_seconds", 15, 3600)
        self._maintain()
        with self._transaction() as db:
            task = self._lease(db, client_id, task_id, lease_token, fence)
            now = self._now()
            if task["lease_expires_at"] <= now or task["deadline"] <= now:
                _error("STALE_LEASE", "Lease expired before renewal; reconcile the task.")
            task["lease_expires_at"] = min(now + lease_seconds, task["deadline"])
            self._save(db, task)
            return self._claim_view(task)

    def checkpoint_task(self, client_id, *, task_id, lease_token, fence, expected_revision, checkpoint):
        _integer(expected_revision, "expected_revision", 0, 2**63 - 1)
        checkpoint = _json(checkpoint, "checkpoint")
        self._maintain()
        with self._transaction() as db:
            task = self._lease(db, client_id, task_id, lease_token, fence)
            if (task.get("_checkpoint_previous_revision") == expected_revision
                    and task.get("_checkpoint_fence") == fence
                    and _hash(task["checkpoint"]) == _hash(checkpoint)):
                return self._view(task)
            if task["revision"] != expected_revision:
                _error("REVISION_CONFLICT", "Task changed; read current state before checkpointing.")
            task["checkpoint"] = checkpoint
            task["_checkpoint_previous_revision"] = expected_revision
            task["_checkpoint_fence"] = fence
            self._changed(db, task)
            return self._view(task)

    def complete_task(self, client_id, *, task_id, lease_token, fence, result,
                      outcome="completed", evidence=None):
        if outcome not in ("completed", "failed", "uncertain", "blocked"):
            _error("INVALID_INPUT", "Unsupported worker outcome.")
        result = _json(result, "result", 262144)
        evidence = _json(evidence, "evidence", optional=True)
        digest = _hash({"outcome": outcome, "result": result, "evidence": evidence})
        self._maintain()
        with self._transaction() as db:
            task = self._lease(db, client_id, task_id, lease_token, fence, terminal_retry=True)
            if task["_completion_hash"]:
                if task["_completion_hash"] != digest:
                    _error("RESULT_CONFLICT", "The original result is immutable; this completion differs.")
                return self._view(task)
            task.update(status=outcome, result=result, evidence=evidence,
                        result_verification="worker_reported", _completion_hash=digest)
            self._changed(db, task)
            return self._view(task)

    def task_status(self, client_id, *, task_id):
        self._maintain()
        with closing(self._connect()) as db:
            _, task = self._visible(db, client_id, task_id)
            return self._view(task)

    def verify_result(self, client_id, *, task_id, expected_revision, accepted, evidence):
        """Sender records its independent review; delivery and worker ACK cannot do so."""
        _integer(expected_revision, "expected_revision", 0, 2**63 - 1)
        if type(accepted) is not bool:
            _error("INVALID_INPUT", "accepted must be a boolean.")
        evidence = _json(evidence, "evidence")
        if not isinstance(evidence.get("checks"), list) or not evidence["checks"]:
            _error("INVALID_EVIDENCE", "Record the result checks actually performed.")
        _text(evidence.get("reference"), "evidence reference", 2048)
        digest = _hash({"accepted": accepted, "evidence": evidence, "revision": expected_revision})
        with self._transaction() as db:
            _, task = self._visible(db, client_id, task_id, sender_only=True)
            if task.get("_verification_hash") == digest:
                return self._view(task)
            if task["revision"] != expected_revision:
                _error("REVISION_CONFLICT", "Task changed; review the current result before verification.")
            if task["status"] != "completed":
                _error("RESULT_NOT_COMPLETED", "Only a saved completed result can be accepted or rejected.")
            task.update(verification_evidence=evidence,
                        result_verification="sender_verified" if accepted else "sender_rejected",
                        acceptance="accepted" if accepted else "rejected",
                        _verification_hash=digest)
            self._changed(db, task)
            return self._view(task)

    def cancel_task(self, client_id, *, task_id, reason):
        _text(reason, "reason", 4096)
        self._maintain()
        with self._transaction() as db:
            _, task = self._visible(db, client_id, task_id, sender_only=True)
            if task["status"] in ("completed", "failed", "cancelled", "expired"):
                return self._view(task)
            if (task["cancel_requested"]
                    and task.get("_cancellation_reason", task.get("status_reason")) == reason):
                # Recover a lost cancellation response without changing the
                # reconciliation revision or emitting another notification.
                return self._view(task)
            task["cancel_requested"] = True
            task["_cancellation_reason"] = reason
            task["status_reason"] = reason
            if task["status"] == "queued":
                task["status"] = "cancelled"
            else:
                task["status"] = "uncertain"
                task["lease_expires_at"] = self._now()
            self._changed(db, task)
            return self._view(task)

    def reconcile_task(self, client_id, *, task_id, decision, evidence, expected_revision):
        _integer(expected_revision, "expected_revision", 0, 2**63 - 1)
        evidence = _json(evidence, "evidence")
        if not evidence or decision not in ("retry", "complete", "fail", "cancel"):
            _error("INVALID_INPUT", "Reconciliation needs a supported decision and recorded evidence.")
        self._maintain()
        with self._transaction() as db:
            _, task = self._visible(db, client_id, task_id, sender_only=True)
            digest = _hash({"decision": decision, "evidence": evidence, "revision": expected_revision})
            if task.get("_reconciliation_hash") == digest:
                return self._view(task)
            if task["revision"] != expected_revision:
                _error("REVISION_CONFLICT", "Task changed; read current state before reconciliation.")
            if task["status"] not in ("uncertain", "blocked"):
                _error("RECONCILIATION_NOT_REQUIRED", "Only blocked or uncertain work can be reconciled.")
            if decision == "retry":
                if evidence.get("effects_absent") is not True:
                    _error("RETRY_REQUIRES_EVIDENCE", "Verify effects are absent before explicitly retrying.")
                if task["deadline"] <= self._now() or task["cancel_requested"]:
                    _error("RETRY_NOT_ALLOWED", "Expired or cancellation-requested work cannot be retried.")
                task.update(status="queued", result=None, _completion_hash=None,
                            _lease_token=None, _claim_request_id=None, lease_expires_at=None)
            else:
                task["status"] = {"complete": "completed", "fail": "failed", "cancel": "cancelled"}[decision]
                task["_lease_token"] = None
            task["reconciliation_evidence"] = evidence
            task["_reconciliation_hash"] = digest
            task["result_verification"] = "sender_reconciled"
            self._changed(db, task, queued=decision == "retry")
            return self._view(task)

    def events_after(self, *, after=0, limit=100):
        _integer(after, "after", 0, 2**63 - 1)
        _integer(limit, "limit", 1, 1000)
        self._maintain()
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM coord_events WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def pending_task_events(self, agent_id, *, after=0, limit=1000):
        """Page current receiver work and sender outcomes by durable sequence.

        Transports persist their own cursor only with the matching outbox
        enqueue. A bounded first page must not hide older disconnected results
        forever when a recipient has more than one page of retained outcomes.
        """
        _integer(after, "after", 0, 2**63 - 1)
        _integer(limit, "limit", 1, 1000)
        self._maintain()
        with closing(self._connect()) as db:
            self._agent(db, agent_id=agent_id)
            rows = db.execute("""SELECT e.* FROM coord_events e JOIN coord_tasks t ON t.task_id=e.task_id
                WHERE e.recipient=? AND e.sequence>? AND (
                    (e.event_type='task.queued' AND t.status='queued' AND t.receiver=e.recipient)
                    OR (e.event_type IN ('task.completed','task.updated') AND t.sender=e.recipient
                        AND t.status IN ('completed','failed','uncertain','blocked','cancelled','expired')))
                AND e.sequence=(SELECT max(n.sequence) FROM coord_events n
                    WHERE n.task_id=e.task_id AND n.recipient=e.recipient)
                ORDER BY e.sequence LIMIT ?""", (agent_id, after, limit)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
