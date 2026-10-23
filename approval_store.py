"""Pending owner approvals for a local, provider-independent OAuth flow.

Pending requests are not grants. Only a locally authorized administrator
may approve after consuming a one-time pairing code.
"""
import secrets
import sqlite3
import time
from pathlib import Path

class ApprovalStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS approvals(
                request_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                resource TEXT NOT NULL,
                scope TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                state TEXT,
                expires_at INTEGER NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0,
                consumed INTEGER NOT NULL DEFAULT 0
            )""")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def create(self, *, client_id: str, redirect_uri: str, resource: str,
               scope: str, code_challenge: str, state: str | None = None,
               now: int | None = None) -> str:
        if not client_id or not redirect_uri or not resource.startswith("https://") or not scope:
            raise ValueError("invalid authorization request")
        if len(code_challenge) != 43:
            raise ValueError("PKCE S256 challenge required")
        now = int(time.time()) if now is None else now
        request_id = secrets.token_urlsafe(24)
        with self._db() as db:
            db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,0,0)",
                       (request_id,client_id,redirect_uri,resource,scope,code_challenge,state,now+300))
        return request_id

    def get(self, request_id: str, *, now: int | None = None) -> dict | None:
        now = int(time.time()) if now is None else now
        with self._db() as db:
            row = db.execute("SELECT client_id,redirect_uri,resource,scope,code_challenge,state,expires_at,approved,consumed FROM approvals WHERE request_id=?",
                             (request_id,)).fetchone()
        if row is None or now >= row[6] or row[8]:
            return None
        return dict(zip(("client_id","redirect_uri","resource","scope","code_challenge","state","expires_at","approved","consumed"),row))

    def approve(self, request_id: str, *, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        with self._db() as db:
            result = db.execute("UPDATE approvals SET approved=1 WHERE request_id=? AND approved=0 AND consumed=0 AND expires_at>?",
                                (request_id,now))
            return result.rowcount == 1

    def consume_approved(self, request_id: str, *, now: int | None = None) -> dict | None:
        now = int(time.time()) if now is None else now
        with self._db() as db:
            row = db.execute("SELECT client_id,redirect_uri,resource,scope,code_challenge,state,expires_at,approved,consumed FROM approvals WHERE request_id=?",
                             (request_id,)).fetchone()
            if row is None or not row[7] or row[8] or now >= row[6]:
                return None
            db.execute("UPDATE approvals SET consumed=1 WHERE request_id=? AND consumed=0", (request_id,))
            return dict(zip(("client_id","redirect_uri","resource","scope","code_challenge","state","expires_at","approved","consumed"),row))
