"""Opt-in native wake delivery, separated from assignment and execution state.

The callback contract and delivery rules reuse the owner's reviewed bridge v1.2:
signed challenges, public-address pinning, durable retries, and stable event IDs.
No deployment configuration or historical identities are included. A callback's
success proves transport acceptance only. It neither verifies its actual host
session nor completes a task; the operator binding gate and Store own those
separate decisions.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
from pathlib import Path
import secrets
import socket
import sqlite3
import ssl
import time
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import h11
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp.types import RequestParams
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from tool_access import current_client_access, read_admin_allowlist
from .store import CoordinationError, Store


PROTOCOL_VERSION = "2026-07-28"
EVENT_NAME = "aster.coordination.changed"
MAX_PAYLOAD_BYTES = 262144
MAX_RESPONSE_BYTES = 65536
MAX_ATTEMPTS = 6
CALLBACK_TIMEOUT = 10.0
DELIVERY_LEASE_SECONDS = 30.0
DEFAULT_TTL_MS = 86400000
MAX_TTL_MS = 7 * DEFAULT_TTL_MS
CATCHUP_PAGE_SIZE = 100
ROTATION_SECONDS = 300
LOGGER = logging.getLogger(__name__)


class CallbackError(ValueError):
    """A bounded public error; never include a URL, secret, or response body."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("Callback endpoint failed: " + reason)


def callback_url(value: str) -> tuple[str, str, int, str]:
    """Return canonical URL, TLS host, port and origin-form HTTP target."""
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError
        if any(ord(c) < 33 or ord(c) > 126 for c in value) or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "%" in host):
            raise ValueError
        port = parsed.port if parsed.port is not None else 443
        if not 1 <= port <= 65535:
            raise ValueError
        host = host.lower().rstrip(".")
        if not host:
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port != 443:
            authority += f":{port}"
        path = parsed.path or "/"
        canonical = urlunsplit(("https", authority, path, parsed.query, ""))
        target = path + ("?" + parsed.query if parsed.query else "")
        return canonical, host, port, target
    except (ValueError, TypeError):
        raise CallbackError("invalid_url") from None


def public_address(address: str) -> bool:
    try:
        if "%" in address:
            return False
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return bool(ip.is_global and not ip.is_multicast and not ip.is_reserved)
    except ValueError:
        return False


def signing_key(secret: str) -> bytes:
    try:
        if not isinstance(secret, str) or not secret.startswith("whsec_"):
            raise ValueError
        encoded = secret[6:]
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        if not 24 <= len(raw) <= 64 or base64.b64encode(raw).decode().rstrip("=") != encoded.rstrip("="):
            raise ValueError
        return raw
    except (ValueError, TypeError):
        raise CallbackError("invalid_signing_secret") from None


def signature_headers(secret: str, subscription_id: str, event_id: str,
                      body: bytes, *, timestamp: int | None = None,
                      previous_secret: str | None = None) -> dict[str, str]:
    """Standard Webhooks v1 signs the exact bytes, ID, and current timestamp."""
    at = int(time.time()) if timestamp is None else timestamp
    signed = event_id.encode("ascii") + b"." + str(at).encode("ascii") + b"." + body
    keys = [signing_key(secret)]
    if previous_secret is not None and (previous_key := signing_key(previous_secret)) not in keys:
        keys.append(previous_key)
    signatures = ["v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode() for key in keys]
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": str(at), "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": subscription_id}


async def public_https_post(url: str, body: bytes, headers: dict[str, str], *,
                            timeout: float = CALLBACK_TIMEOUT,
                            resolver=None, connector=None) -> tuple[int, bytes]:
    """One HTTPS request, no redirects/proxies/pooling or second DNS lookup.

    DNS resolution, TLS, request and bounded response share a hard deadline.
    Test seams replace DNS/connection only; production uses the event loop's
    resolver and TLS streams with the original hostname for certificate checks.
    """
    _, host, port, target = callback_url(url)
    if len(body) > MAX_PAYLOAD_BYTES:
        raise CallbackError("payload_too_large")
    writer = None
    try:
        async with asyncio.timeout(timeout):
            resolve = resolver or asyncio.get_running_loop().getaddrinfo
            try:
                addresses = await resolve(host, port, type=socket.SOCK_STREAM)
            except OSError:
                raise CallbackError("dns_failed") from None
            if not addresses or any(not public_address(answer[4][0]) for answer in addresses):
                raise CallbackError("blocked_address")
            family, _, _, _, address = addresses[0]
            connect = connector or asyncio.open_connection
            reader, writer = await connect(
                host=address[0], port=port, family=family,
                ssl=ssl.create_default_context(), server_hostname=host,
            )
            authority = f"[{host}]" if ":" in host else host
            if port != 443:
                authority += f":{port}"
            # Caller-controlled framing/Host headers are never forwarded.
            safe = [(k.encode("ascii"), v.encode("ascii")) for k, v in headers.items()
                    if k.lower() not in {"host", "content-length", "transfer-encoding", "connection"}]
            wire = h11.Connection(h11.CLIENT, max_incomplete_event_size=MAX_RESPONSE_BYTES)
            request = h11.Request(method=b"POST", target=target.encode("ascii"), headers=[
                (b"Host", authority.encode("ascii")), (b"Content-Length", str(len(body)).encode()),
                (b"Connection", b"close"), *safe])
            writer.write(wire.send(request) + wire.send(h11.Data(data=body)) + wire.send(h11.EndOfMessage()))
            await writer.drain()
            status = None
            chunks: list[bytes] = []
            size = 0
            wire_size = 0
            while True:
                event = wire.next_event()
                if event is h11.NEED_DATA:
                    data = await reader.read(8192)
                    wire_size += len(data)
                    if wire_size > 2 * MAX_RESPONSE_BYTES:
                        raise CallbackError("response_too_large")
                    wire.receive_data(data)
                elif isinstance(event, h11.Response):
                    status = event.status_code
                elif isinstance(event, h11.InformationalResponse):
                    continue
                elif isinstance(event, h11.Data):
                    size += len(event.data)
                    if size > MAX_RESPONSE_BYTES:
                        raise CallbackError("response_too_large")
                    chunks.append(bytes(event.data))
                elif isinstance(event, h11.EndOfMessage):
                    if status is None:
                        raise CallbackError("invalid_response")
                    return status, b"".join(chunks)
                else:
                    raise CallbackError("invalid_response")
    except TimeoutError:
        raise CallbackError("timeout") from None
    except (OSError, h11.ProtocolError, UnicodeError):
        raise CallbackError("network_error") from None
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except (OSError, TimeoutError):
                pass


Post = Callable[[str, bytes, dict[str, str]], Awaitable[tuple[int, bytes]]]


async def verify_callback(url: str, secret: str, subscription_id: str, post: Post,
                          *, timestamp: int | None = None) -> None:
    challenge = secrets.token_urlsafe(32)
    body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
    event_id = "verification_" + secrets.token_hex(16)
    status, response = await post(url, body, signature_headers(
        secret, subscription_id, event_id, body, timestamp=timestamp))
    try:
        echoed = json.loads(response).get("challenge")
    except (ValueError, AttributeError, UnicodeError):
        echoed = None
    if (not 200 <= status < 300 or not isinstance(echoed, str)
            or not hmac.compare_digest(echoed.encode(), challenge.encode())):
        raise CallbackError("challenge_failed")


class EventArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1, max_length=128)
    expected_session_id: str = Field(min_length=1, max_length=256)


class WebhookDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["webhook"]
    url: str = Field(max_length=4096)
    secret: str = Field(min_length=1, max_length=256, repr=False)


class WebhookIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["webhook"]
    url: str = Field(max_length=4096)
    secret: str | None = Field(default=None, max_length=256, repr=False)


class SubscribeParams(RequestParams):
    model_config = ConfigDict(extra="forbid")
    name: Literal[EVENT_NAME]
    arguments: EventArguments
    delivery: WebhookDelivery
    ttl_ms: StrictInt | None = Field(default=None, alias="ttlMs", gt=0)
    # This event has no protocol replay cursor. Durable task-level catchup is
    # server-owned and must not be confused with acknowledging an event stream.
    cursor: None = None


class UnsubscribeParams(RequestParams):
    model_config = ConfigDict(extra="forbid")
    name: Literal[EVENT_NAME]
    arguments: EventArguments
    delivery: WebhookIdentity


EVENT_DEFINITION = {
    "name": EVENT_NAME,
    "description": (
        "Changes addressed to this registered agent. Enrollment first delivers a content-free probe. "
        "Task notices require operator verification of the actual host destination. "
        "For receiver notices, use aster_task_status and claim only that exact queued assignment. "
        "For sender notices, use aster_outcomes or aster_task_status to inspect saved outcomes; "
        "never claim a completed sender task. "
        "Task status is authoritative because notices may be stale or repeated. "
        "HTTP 200 acknowledges transport only, not execution, completion or a visible user notification."
    ),
    "delivery": ["webhook"],
    "inputSchema": EventArguments.model_json_schema(),
    "payloadSchema": {"type": "object", "properties": {
        "agent_id": {"type": "string"}, "event_type": {"type": "string"},
        "task_id": {"type": ["string", "null"]}, "status": {"type": ["string", "null"]},
        "probe_id": {"type": ["string", "null"]},
        "revision": {"type": ["integer", "null"], "minimum": 0},
        "recipient_role": {"type": ["string", "null"], "enum": ["sender", "receiver", None]}},
        # Retained outbox bodies predate role/revision metadata. Keep these
        # additions optional so retries preserve their original signed bytes.
        "required": ["agent_id", "event_type", "task_id", "status", "probe_id"],
        "additionalProperties": False},
}


NATIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS coord_native_subscriptions (
 id TEXT PRIMARY KEY, identity_key TEXT NOT NULL, agent_id TEXT NOT NULL,
 client_id TEXT NOT NULL, expected_session_id TEXT NOT NULL, url TEXT NOT NULL,
 secret TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 active INTEGER NOT NULL, version INTEGER NOT NULL,
 catchup_sequence INTEGER NOT NULL DEFAULT 0, previous_secret TEXT,
 rotation_until REAL, delivery_epoch INTEGER NOT NULL DEFAULT 1,
 binding_generation INTEGER);
CREATE INDEX IF NOT EXISTS coord_native_agent ON coord_native_subscriptions(agent_id,active);
CREATE TABLE IF NOT EXISTS coord_native_outbox (
 subscription_id TEXT NOT NULL, event_id TEXT NOT NULL, body TEXT NOT NULL,
 kind TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL, lease_token TEXT, lease_until REAL, last_status INTEGER,
 last_reason TEXT, created_at REAL NOT NULL, delivery_epoch INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(subscription_id,event_id));
CREATE INDEX IF NOT EXISTS coord_native_due ON coord_native_outbox(status,next_at);
CREATE TABLE IF NOT EXISTS coord_native_cursor (id INTEGER PRIMARY KEY, sequence INTEGER NOT NULL);
INSERT OR IGNORE INTO coord_native_cursor VALUES (1,0);
"""


class NativeEvents:
    """Durable native subscription and transport state alongside Store's tables."""

    def __init__(self, store: Store, *, allowlist_path: str | Path,
                 oauth_db_path: str | Path, post: Post = public_https_post,
                 clock: Callable[[], float] | None = None, interval: float = 1.0):
        self.store = store
        self.path = store.path
        self.allowlist_path = Path(allowlist_path)
        self.oauth_db_path = Path(oauth_db_path)
        self.post = post
        self.clock = clock or store.clock
        self.interval = interval
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._dispatch_offset = 0
        with self._db() as db:
            db.executescript(NATIVE_SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(coord_native_subscriptions)")}
            if "catchup_sequence" not in columns:
                db.execute("ALTER TABLE coord_native_subscriptions ADD COLUMN catchup_sequence INTEGER NOT NULL DEFAULT 0")
            for name, definition in (("previous_secret", "TEXT"), ("rotation_until", "REAL"),
                                     ("delivery_epoch", "INTEGER NOT NULL DEFAULT 1"), ("binding_generation", "INTEGER")):
                if name not in columns:
                    db.execute(f"ALTER TABLE coord_native_subscriptions ADD COLUMN {name} {definition}")
            outbox_columns = {row[1] for row in db.execute("PRAGMA table_info(coord_native_outbox)")}
            if "delivery_epoch" not in outbox_columns:
                db.execute("ALTER TABLE coord_native_outbox ADD COLUMN delivery_epoch INTEGER NOT NULL DEFAULT 1")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._db() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _authorized(self, client_id: str) -> bool:
        try:
            if client_id not in read_admin_allowlist(self.allowlist_path):
                return False
            # A missing/unreadable OAuth DB must not silently create a new one.
            uri = self.oauth_db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                row = db.execute("SELECT revoked FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
            return bool(row and not row[0])
        except Exception:
            return False

    def _own_agent(self, client_id: str, args: EventArguments) -> dict:
        if not self._authorized(client_id):
            raise CoordinationError("NATIVE_NOT_AUTHORIZED", "Current operator grant and active OAuth client are required.")
        agent = self.store.agent_for_client(client_id)
        if agent["agent_id"] != args.agent_id or agent["receive_mode"] != "native":
            raise CoordinationError("NATIVE_NOT_AUTHORIZED", "The subscription must name this client's native receiver.")
        return agent

    def _subscription(self, subscription_id: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM coord_native_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            return dict(row) if row else None

    async def subscribe(self, client_id: str, params: SubscribeParams | dict) -> dict:
        p = params if isinstance(params, SubscribeParams) else SubscribeParams.model_validate(params)
        self._own_agent(client_id, p.arguments)
        url, _, _, _ = callback_url(p.delivery.url)
        key = signing_key(p.delivery.secret)
        # The official Events contract makes key rotation a refresh of the
        # same principal/URL/event/arguments identity, not a new destination.
        identity = json.dumps({"client": client_id, "url": url, "name": p.name,
            "arguments": p.arguments.model_dump()}, sort_keys=True, separators=(",", ":"))
        identity_key = hashlib.sha256(identity.encode()).hexdigest()
        subscription_id = "sub_" + identity_key
        at = self.clock()
        with self._db() as db:
            row = db.execute("SELECT * FROM coord_native_subscriptions WHERE agent_id=? AND active=1", (p.arguments.agent_id,)).fetchone()
            target = db.execute("SELECT * FROM coord_native_subscriptions WHERE id=?", (subscription_id,)).fetchone()
        previous = dict(row) if row else None
        target = dict(target) if target else None
        # A replacement key must pass its OWN fresh challenge, signed only by
        # that key. Dual signing starts afterwards; the old key cannot make a
        # failed replacement-key verification look successful.
        await verify_callback(url, p.delivery.secret, subscription_id, self.post, timestamp=int(at))
        agent = self._own_agent(client_id, p.arguments)
        at = self.clock()
        old_binding = self.store.native_binding(p.arguments.agent_id)
        renewable = bool(previous and previous["id"] == subscription_id and previous["expires_at"] > at
            and old_binding and old_binding["active"] and old_binding["expires_at"] > at
            and old_binding["subscription_id"] == subscription_id
            and old_binding["expected_session_id"] == p.arguments.expected_session_id
            and old_binding["generation"] == agent["generation"])
        epoch = target["delivery_epoch"] if renewable else (target["delivery_epoch"] + 1 if target else 1)
        previous_secret = None
        rotation_until = None
        if renewable and signing_key(previous["secret"]) != key:
            previous_secret, rotation_until = previous["secret"], at + ROTATION_SECONDS
        elif renewable and previous["previous_secret"] and (previous["rotation_until"] or 0) > at:
            # Repeating an identical refresh must not extend the old key's life.
            previous_secret, rotation_until = previous["previous_secret"], previous["rotation_until"]
        expires = at + min(p.ttl_ms or DEFAULT_TTL_MS, MAX_TTL_MS) / 1000
        with self._transaction() as db:
            current = db.execute("SELECT * FROM coord_native_subscriptions WHERE agent_id=? AND active=1", (p.arguments.agent_id,)).fetchone()
            if ((current is None) != (previous is None)
                    or current and (current["id"], current["version"]) != (previous["id"], previous["version"])):
                raise CoordinationError("SUBSCRIPTION_CHANGED", "Subscription changed during verification; retry.")
            db.execute("UPDATE coord_native_subscriptions SET active=0,secret=NULL,previous_secret=NULL,rotation_until=NULL,version=version+1 WHERE agent_id=? AND id<>?", (p.arguments.agent_id, subscription_id))
            db.execute("UPDATE coord_native_outbox SET status='cancelled',lease_token=NULL WHERE subscription_id IN (SELECT id FROM coord_native_subscriptions WHERE agent_id=? AND active=0) AND status IN ('pending','inflight')", (p.arguments.agent_id,))
            if not renewable:
                db.execute("UPDATE coord_native_outbox SET status='cancelled',lease_token=NULL WHERE subscription_id=? AND status IN ('pending','inflight')", (subscription_id,))
            version = target["version"] + 1 if target else 1
            db.execute("""INSERT INTO coord_native_subscriptions
                (id,identity_key,agent_id,client_id,expected_session_id,url,secret,created_at,expires_at,active,version,
                 catchup_sequence,previous_secret,rotation_until,delivery_epoch,binding_generation)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,expires_at=excluded.expires_at,
                active=1,version=excluded.version,created_at=excluded.created_at,catchup_sequence=excluded.catchup_sequence,
                previous_secret=excluded.previous_secret,rotation_until=excluded.rotation_until,
                delivery_epoch=excluded.delivery_epoch,binding_generation=excluded.binding_generation""",
                (subscription_id, identity_key, p.arguments.agent_id,
                client_id, p.arguments.expected_session_id, url, p.delivery.secret,
                previous["created_at"] if renewable else at, expires, 1, version,
                previous["catchup_sequence"] if renewable else 0, previous_secret, rotation_until, epoch,
                old_binding["generation"] if renewable else None))
        try:
            binding = self.store.bind_receiver(agent_id=p.arguments.agent_id,
                subscription_id=subscription_id, expected_session_id=p.arguments.expected_session_id,
                expires_at=expires)
        except Exception:
            self._deactivate(subscription_id)
            raise
        with self._transaction() as db:
            current = db.execute("SELECT version FROM coord_native_subscriptions WHERE id=? AND active=1", (subscription_id,)).fetchone()
            if not current or current["version"] != version:
                raise CoordinationError("SUBSCRIPTION_CHANGED", "Binding changed during subscription; retry.")
            # An operator may revoke/replace the binding while the fresh
            # challenge is in flight. Store decides the actual generation.
            if renewable and binding["generation"] != old_binding["generation"]:
                epoch += 1
                db.execute("UPDATE coord_native_subscriptions SET delivery_epoch=?,catchup_sequence=0,created_at=?,previous_secret=NULL,rotation_until=NULL WHERE id=?", (epoch, at, subscription_id))
                db.execute("UPDATE coord_native_outbox SET status='cancelled',lease_token=NULL WHERE subscription_id=? AND status IN ('pending','inflight')", (subscription_id,))
            db.execute("UPDATE coord_native_subscriptions SET binding_generation=? WHERE id=?", (binding["generation"], subscription_id))
        latest = self._subscription(subscription_id)
        if not latest or not latest["active"] or latest["version"] != version:
            raise CoordinationError("SUBSCRIPTION_CHANGED", "Binding changed during subscription; retry.")
        probe_id = None
        if not binding["operator_verified"]:
            probe_id = "probe_" + secrets.token_hex(16)
            self._enqueue(subscription_id, self._envelope(probe_id, at, p.arguments.agent_id,
                "receiver.probe", None, "verification_required", probe_id), "probe", epoch=epoch)
        self._catch_up(latest)
        return {"id": subscription_id, "refreshBefore": _iso(expires), "cursor": None,
                "truncated": False, "bindingVerified": binding["operator_verified"],
                "probeEventId": probe_id}

    def unsubscribe(self, client_id: str, params: UnsubscribeParams | dict) -> dict:
        p = params if isinstance(params, UnsubscribeParams) else UnsubscribeParams.model_validate(params)
        self._own_agent(client_id, p.arguments)
        url, _, _, _ = callback_url(p.delivery.url)
        with self._db() as db:
            rows = db.execute("SELECT id FROM coord_native_subscriptions WHERE client_id=? AND agent_id=? AND expected_session_id=? AND url=? AND active=1", (client_id, p.arguments.agent_id, p.arguments.expected_session_id, url)).fetchall()
        for row in rows:
            self._deactivate(row["id"])
            try:
                self.store.revoke_receiver(p.arguments.agent_id, row["id"])
            except CoordinationError as error:
                if error.code != "STALE_BINDING":
                    raise
        return {}

    def _deactivate(self, subscription_id: str):
        with self._transaction() as db:
            db.execute("UPDATE coord_native_subscriptions SET active=0,secret=NULL,previous_secret=NULL,rotation_until=NULL,version=version+1 WHERE id=?", (subscription_id,))
            db.execute("UPDATE coord_native_outbox SET status='cancelled',lease_token=NULL WHERE subscription_id=? AND status IN ('pending','inflight')", (subscription_id,))

    @staticmethod
    def _envelope(event_id, at, agent_id, event_type, task_id, status, probe_id=None,
                  *, revision=None, recipient_role=None):
        return {"eventId": event_id, "name": EVENT_NAME, "timestamp": _iso(at), "cursor": None,
                "data": {"agent_id": agent_id, "event_type": event_type,
                         "task_id": task_id, "status": status, "probe_id": probe_id,
                         "revision": revision, "recipient_role": recipient_role}}

    def _task_envelope(self, event, subscription):
        # A notification's delivery epoch changes on fresh receiver enrollment,
        # not on key rotation. The durable work ID and claim semantics do not.
        identity = json.dumps([subscription["id"], subscription["delivery_epoch"], event["event_id"]], separators=(",", ":"))
        event_id = "evt_" + hashlib.sha256(identity.encode()).hexdigest()
        payload = event["payload"]
        # Route from the stored event snapshot, not the task's later state.
        # Queued assignments address receivers; progress/outcomes address senders.
        if event["event_type"] == "task.queued" and event["recipient"] == payload.get("receiver"):
            recipient_role = "receiver"
        elif event["recipient"] == payload.get("sender"):
            recipient_role = "sender"
        elif event["recipient"] == payload.get("receiver"):
            recipient_role = "receiver"
        else:
            recipient_role = None
        return self._envelope(event_id, event["created_at"], event["recipient"],
            event["event_type"], event["task_id"], payload.get("status"),
            revision=payload.get("revision"), recipient_role=recipient_role)

    def _enqueue(self, subscription_id, envelope, kind, *, epoch, db=None):
        values = (subscription_id, envelope["eventId"], json.dumps(envelope, separators=(",", ":")),
                  kind, "pending", self.clock(), self.clock(), epoch, subscription_id, epoch)
        # The epoch check is in the enqueue transaction: a slow catchup pass
        # cannot reinsert old-epoch notifications after a concurrent rebind.
        statement = """INSERT OR IGNORE INTO coord_native_outbox
            (subscription_id,event_id,body,kind,status,next_at,created_at,delivery_epoch)
            SELECT ?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM coord_native_subscriptions
                WHERE id=? AND active=1 AND delivery_epoch=?)"""
        if db is None:
            with self._transaction() as connection:
                connection.execute(statement, values)
        else:
            db.execute(statement, values)

    def _catch_up(self, subscription):
        # Store calls are deliberately outside native SQLite write transactions.
        try:
            events = self.store.pending_task_events(subscription["agent_id"],
                after=subscription["catchup_sequence"], limit=CATCHUP_PAGE_SIZE)
        except CoordinationError:
            return
        with self._transaction() as db:
            for event in events:
                self._enqueue(subscription["id"], self._task_envelope(event, subscription), "task",
                              epoch=subscription["delivery_epoch"], db=db)
            if events:
                db.execute("UPDATE coord_native_subscriptions SET catchup_sequence=max(catchup_sequence,?) WHERE id=? AND delivery_epoch=?",
                           (events[-1]["sequence"], subscription["id"], subscription["delivery_epoch"]))

    def _sync(self):
        with self._db() as db:
            cursor = db.execute("SELECT sequence FROM coord_native_cursor WHERE id=1").fetchone()[0]
            subscriptions = [dict(r) for r in db.execute("SELECT * FROM coord_native_subscriptions WHERE active=1 AND expires_at>?", (self.clock(),))]
        events = self.store.events_after(after=cursor, limit=100)
        with self._transaction() as db:
            for event in events:
                for sub in subscriptions:
                    if event["recipient"] == sub["agent_id"]:
                        self._enqueue(sub["id"], self._task_envelope(event, sub), "task", epoch=sub["delivery_epoch"], db=db)
            if events:
                db.execute("UPDATE coord_native_cursor SET sequence=max(sequence,?) WHERE id=1", (events[-1]["sequence"],))
        # Recovers work committed before a subscription, including offline results.
        for sub in subscriptions:
            self._catch_up(sub)

    def _ready(self, sub, kind):
        if not sub or not sub["active"] or sub["expires_at"] <= self.clock() or not sub["secret"]:
            return False
        if not self._authorized(sub["client_id"]):
            return False
        try:
            agent = self.store.agent_for_client(sub["client_id"])
            binding = self.store.native_binding(sub["agent_id"])
        except CoordinationError:
            return False
        if (agent["agent_id"] != sub["agent_id"] or agent["receive_mode"] != "native"
                or not binding or not binding["active"] or binding["expires_at"] <= self.clock()
                or binding["subscription_id"] != sub["id"]
                or binding["expected_session_id"] != sub["expected_session_id"]
                or binding["generation"] != sub["binding_generation"]
                or binding["generation"] != agent["generation"]):
            return False
        return kind == "probe" or binding["ready"] is True

    def receipts(self, client_id: str, subscription_id: str) -> list[dict]:
        """Operator/test inspection; deliberately excludes URL, key and payload."""
        sub = self._subscription(subscription_id)
        if not sub or sub["client_id"] != client_id or not self._authorized(client_id):
            raise CoordinationError("NATIVE_NOT_AUTHORIZED", "Subscription is not accessible.")
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT event_id,kind,status,attempts,last_status,last_reason,delivery_epoch FROM coord_native_outbox WHERE subscription_id=? ORDER BY created_at,event_id", (subscription_id,))]

    async def run_once(self):
        async with self._lock:
            self._sync()
            with self._transaction() as db:
                db.execute("""UPDATE coord_native_outbox SET status='failed',last_reason='attempt_outcome_unknown',
                    lease_token=NULL,lease_until=NULL WHERE status='inflight' AND lease_until<=? AND attempts>=?""",
                    (self.clock(), MAX_ATTEMPTS))
                db.execute("UPDATE coord_native_subscriptions SET active=0,secret=NULL,previous_secret=NULL,rotation_until=NULL,version=version+1 WHERE active=1 AND expires_at<=?", (self.clock(),))
                db.execute("UPDATE coord_native_subscriptions SET previous_secret=NULL,rotation_until=NULL WHERE rotation_until<=?", (self.clock(),))
                db.execute("UPDATE coord_native_outbox SET status='cancelled',lease_token=NULL WHERE status IN ('pending','inflight') AND subscription_id IN (SELECT id FROM coord_native_subscriptions WHERE active=0)")
            with self._db() as db:
                subscriptions = [dict(r) for r in db.execute("SELECT * FROM coord_native_subscriptions WHERE active=1 AND expires_at>? ORDER BY id", (self.clock(),))]
            groups = []
            for sub in subscriptions:
                task_ready = self._ready(sub, "task")
                if not task_ready and not self._ready(sub, "probe"):
                    continue
                with self._db() as db:
                    rows = db.execute("""SELECT * FROM coord_native_outbox WHERE subscription_id=?
                        AND delivery_epoch=? AND (? OR kind='probe') AND ((status='pending' AND next_at<=?)
                        OR (status='inflight' AND lease_until<=?)) ORDER BY next_at,event_id LIMIT 20""",
                        (sub["id"], sub["delivery_epoch"], task_ready, self.clock(), self.clock())).fetchall()
                    if rows:
                        groups.append([dict(r) for r in rows])
            # A blocked destination cannot occupy the global head of the queue.
            # Interleave eligible receivers, rotating the starting receiver.
            if groups:
                offset = self._dispatch_offset % len(groups)
                groups = groups[offset:] + groups[:offset]
                self._dispatch_offset += 1
            due = [group[index] for index in range(20) for group in groups if index < len(group)]
            delivered = 0
            for item in due:
                sub = self._subscription(item["subscription_id"])
                if not self._ready(sub, item["kind"]) or item["delivery_epoch"] != sub["delivery_epoch"]:
                    continue
                lease = secrets.token_hex(24)
                with self._transaction() as db:
                    changed = db.execute("""UPDATE coord_native_outbox SET status='inflight',
                        attempts=attempts+1,lease_token=?,lease_until=? WHERE subscription_id=? AND event_id=?
                        AND attempts<? AND ((status='pending' AND next_at<=?) OR (status='inflight' AND lease_until<=?))""",
                        (lease, self.clock() + DELIVERY_LEASE_SECONDS, sub["id"], item["event_id"],
                         MAX_ATTEMPTS, self.clock(), self.clock())).rowcount
                if not changed:
                    continue
                status = None
                reason = None
                try:
                    body = item["body"].encode()
                    previous_secret = sub["previous_secret"] if (sub["rotation_until"] or 0) > self.clock() else None
                    status, _ = await self.post(sub["url"], body, signature_headers(
                        sub["secret"], sub["id"], item["event_id"], body,
                        timestamp=int(self.clock()), previous_secret=previous_secret))
                except CallbackError as error:
                    reason = error.reason
                except Exception:
                    reason = "network_error"
                accepted = status is not None and 200 <= status < 300
                retryable = status is None or status in (408, 429) or status >= 500
                current = self._subscription(sub["id"])
                still_ready = self._ready(current, item["kind"]) and item["delivery_epoch"] == current["delivery_epoch"]
                with self._transaction() as db:
                    row = db.execute("SELECT attempts FROM coord_native_outbox WHERE subscription_id=? AND event_id=? AND lease_token=?", (sub["id"], item["event_id"], lease)).fetchone()
                    if row:
                        attempts = row[0]
                        state = ("cancelled" if not still_ready else "accepted" if accepted else
                                 "pending" if retryable and attempts < MAX_ATTEMPTS else "failed")
                        db.execute("""UPDATE coord_native_outbox SET status=?,last_status=?,last_reason=?,
                            next_at=?,lease_token=NULL,lease_until=NULL WHERE subscription_id=? AND event_id=? AND lease_token=?""",
                            (state, status, reason, self.clock() + min(300, 2 ** attempts),
                             sub["id"], item["event_id"], lease))
                if status == 410:
                    self._deactivate(sub["id"])
                    try:
                        self.store.revoke_receiver(sub["agent_id"], sub["id"])
                    except CoordinationError:
                        pass
                delivered += 1
                if delivered >= 20:
                    break

    @asynccontextmanager
    async def lifespan(self):
        if self._worker is not None:
            raise RuntimeError("Native dispatcher already started")

        async def dispatch():
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # No credential, callback response or task body in logs.
                    LOGGER.warning("Native delivery pass failed; durable work remains pending")
                await asyncio.sleep(self.interval)

        self._worker = asyncio.create_task(dispatch(), name="aster-native-dispatch")
        try:
            yield self
        finally:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class NativeEventsExtension(Extension):
    identifier = "org.aster-mcp/native-events"

    def __init__(self, native: NativeEvents):
        self.native = native

    def settings(self):
        return {"delivery": ["webhook"], "event": EVENT_NAME}

    def _client(self):
        access = current_client_access(self.native.allowlist_path)
        access.require_admin()
        return access.client_id

    async def _list(self, ctx, params):
        self._client()
        return {"events": [EVENT_DEFINITION]}

    async def _subscribe(self, ctx, params):
        try:
            return await self.native.subscribe(self._client(), params)
        except CallbackError as error:
            raise MCPError(code=-32015, message=str(error), data={"reason": error.reason}) from None
        except CoordinationError as error:
            raise MCPError(code=-32024, message=str(error)) from None

    async def _unsubscribe(self, ctx, params):
        try:
            return self.native.unsubscribe(self._client(), params)
        except (CoordinationError, CallbackError) as error:
            raise MCPError(code=-32024, message=str(error)) from None

    def methods(self):
        versions = frozenset({PROTOCOL_VERSION})
        return [MethodBinding("events/list", RequestParams, self._list, versions),
                MethodBinding("events/subscribe", SubscribeParams, self._subscribe, versions),
                MethodBinding("events/unsubscribe", UnsubscribeParams, self._unsubscribe, versions)]


async def _discover_events(ctx, call_next):
    result = await call_next(ctx)
    if ctx.method == "server/discover" and ctx.protocol_version == PROTOCOL_VERSION:
        result = result.model_dump(by_alias=True, mode="json", exclude_none=True) if isinstance(result, BaseModel) else dict(result)
        result["capabilities"] = {**result.get("capabilities", {}), "events": {}}
        result.update(ttlMs=60000, cacheScope="private")
    return result


def install_native_events(server, native: NativeEvents) -> None:
    """Attach to the existing OAuth MCP server; never create an alternate route.

    MCP 2.3 exposes custom method and middleware APIs. The composition object's
    lowlevel-server reference is the single pinned-version integration seam;
    no SDK authentication, origin/header checks, or existing lifespan is replaced.
    Call before constructing/starting the HTTP application.
    """
    lowlevel = server._lowlevel_server
    extension = NativeEventsExtension(native)
    if extension.identifier in lowlevel.extensions:
        raise ValueError("Native events already installed")
    for method in extension.methods():
        if lowlevel.get_request_handler(method.method) is not None:
            raise ValueError("Native event method already installed")

        async def handler(ctx, params, binding=method):
            if ctx.protocol_version not in binding.protocol_versions:
                raise MCPError(code=-32601, message="Method not found")
            return await binding.handler(ctx, params)

        lowlevel.add_request_handler(method.method, method.params_type, handler)
    lowlevel.extensions[extension.identifier] = extension.settings()
    server.middleware.append(_discover_events)
    previous_lifespan = lowlevel.lifespan

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application) as context:
            async with native.lifespan():
                yield context

    lowlevel.lifespan = lifespan
