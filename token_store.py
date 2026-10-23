"""Persistent, revocable, resource-bound bearer-token store.

Not a complete OAuth authorization server. Tokens must only be issued
after a separately verified owner authorization and PKCE code exchange.
"""
import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

class TokenStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS access_tokens(
                digest TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                resource TEXT NOT NULL, scope TEXT NOT NULL,
                expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            )""")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def issue(self, *, client_id: str, resource: str, scope: str,
              ttl: int = 900, now: int | None = None) -> str:
        if not client_id or not resource.startswith("https://") or not scope or not 1 <= ttl <= 3600:
            raise ValueError("invalid token parameters")
        token = secrets.token_urlsafe(48)
        now = int(time.time()) if now is None else now
        with self._db() as db:
            db.execute("INSERT INTO access_tokens VALUES (?,?,?,?,?,0)",
                       (_hash(token), client_id, resource, scope, now + ttl))
        return token

    def validate(self, token: str, *, resource: str, scope: str,
                 now: int | None = None) -> str | None:
        if not token or not resource or not scope:
            return None
        now = int(time.time()) if now is None else now
        with self._db() as db:
            row = db.execute("SELECT client_id,resource,scope,expires_at,revoked FROM access_tokens WHERE digest=?",
                             (_hash(token),)).fetchone()
        if row is None:
            return None
        client, audience, granted, expiry, revoked = row
        if revoked or now >= expiry or audience != resource or scope not in granted.split():
            return None
        return client

    def revoke(self, token: str) -> None:
        with self._db() as db:
            db.execute("UPDATE access_tokens SET revoked=1 WHERE digest=?", (_hash(token),))

    def revoke_client(self, client_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE access_tokens SET revoked=1 WHERE client_id=?", (client_id,))
