"""Durable, single-use rotating refresh tokens for locally paired MCP clients."""
import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

REFRESH_TTL = 30 * 86400

def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

class RefreshStore:
    def __init__(self, path: str):
        self.path=path
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS refresh_tokens(
                digest TEXT PRIMARY KEY,client_id TEXT NOT NULL,
                resource TEXT NOT NULL,scope TEXT NOT NULL,
                expires_at INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0)""")

    def _db(self):
        db=sqlite3.connect(self.path,timeout=10,isolation_level="IMMEDIATE")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def issue(self,client_id: str,resource: str,scope: str,ttl: int=REFRESH_TTL,now: int|None=None)->str:
        if not client_id or not resource.startswith("https://") or not scope or not 60<=ttl<=REFRESH_TTL:
            raise ValueError("invalid refresh token parameters")
        token=secrets.token_urlsafe(48)
        now=int(time.time()) if now is None else now
        with self._db() as db:
            db.execute("INSERT INTO refresh_tokens VALUES (?,?,?,?,?,0)",
                       (digest(token),client_id,resource,scope,now+ttl))
        return token

    def lookup(self,token: str,client_id: str,now: int|None=None)->dict|None:
        now=int(time.time()) if now is None else now
        with self._db() as db:
            row=db.execute("SELECT client_id,resource,scope,expires_at,used FROM refresh_tokens WHERE digest=?",
                           (digest(token),)).fetchone()
        if not row or row[0]!=client_id or row[4] or now>=row[3]:
            return None
        return dict(zip(("client_id","resource","scope","expires_at","used"),row))

    def consume(self,token: str,client_id: str,now: int|None=None)->dict|None:
        now=int(time.time()) if now is None else now
        with self._db() as db:
            row=db.execute("""UPDATE refresh_tokens SET used=1
                WHERE digest=? AND client_id=? AND used=0 AND expires_at>?
                RETURNING client_id,resource,scope,expires_at""",
                (digest(token),client_id,now)).fetchone()
        return dict(zip(("client_id","resource","scope","expires_at"),row)) if row else None

    def rotate(self, token: str, client_id: str, resource: str, scope: str,
               *, access_ttl: int) -> tuple[str, str] | None:
        """Commit consumption and both replacements as one durable exchange."""
        access = secrets.token_urlsafe(48)
        replacement = secrets.token_urlsafe(48)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            consumed = db.execute(
                """UPDATE refresh_tokens SET used=1
                   WHERE digest=? AND client_id=? AND resource=? AND used=0 AND expires_at>?
                     AND EXISTS (SELECT 1 FROM oauth_clients WHERE client_id=? AND revoked=0)
                   RETURNING scope""",
                (digest(token), client_id, resource, now, client_id),
            ).fetchone()
            if consumed is None:
                return None
            if not set(scope.split()).issubset(consumed[0].split()):
                raise PermissionError("scope escalation rejected")
            db.execute("INSERT INTO access_tokens VALUES (?,?,?,?,?,0)",
                       (digest(access), client_id, resource, scope, now+access_ttl))
            db.execute("INSERT INTO refresh_tokens VALUES (?,?,?,?,?,0)",
                       (digest(replacement), client_id, resource, scope, now+REFRESH_TTL))
        return access, replacement

    def revoke_client(self,client_id: str)->None:
        with self._db() as db:
            db.execute("UPDATE refresh_tokens SET used=1 WHERE client_id=?",(client_id,))

    def revoke(self,token: str)->None:
        """Invalidate one refresh credential; unknown/already-used tokens are harmless."""
        with self._db() as db:
            db.execute("UPDATE refresh_tokens SET used=1 WHERE digest=?",(digest(token),))
