"""Persistent authorization-code store for OAuth PKCE.

A separate, authenticated owner-approval step must create records.
No public issuance endpoint is provided by this module.
"""
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()

def pkce_s256(verifier: str) -> str:
    import base64
    if not 43 <= len(verifier) <= 128:
        raise ValueError("invalid PKCE verifier length")
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

class AuthorizationCodeStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS auth_codes (
                digest TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL, resource TEXT NOT NULL,
                scope TEXT NOT NULL, challenge TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )""")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def issue_approved(self, *, client_id: str, redirect_uri: str, resource: str,
                       scope: str, code_challenge: str, ttl: int = 120,
                       now: int | None = None) -> str:
        if not client_id or not redirect_uri.startswith("https://") or not resource.startswith("https://"):
            raise ValueError("invalid client or URI")
        if not scope or len(code_challenge) != 43 or not 1 <= ttl <= 300:
            raise ValueError("invalid scope, challenge or lifetime")
        now = int(time.time()) if now is None else now
        code = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("INSERT INTO auth_codes VALUES (?,?,?,?,?,?,?)",
                       (_digest(code), client_id, redirect_uri, resource,
                        scope, code_challenge, now + ttl))
        return code

    def consume(self, code: str, *, client_id: str, redirect_uri: str,
                resource: str, verifier: str, now: int | None = None) -> str | None:
        try:
            computed = pkce_s256(verifier)
        except (ValueError, UnicodeEncodeError):
            return None
        now = int(time.time()) if now is None else now
        with self._db() as db:
            row = db.execute("SELECT client_id,redirect_uri,resource,scope,challenge,expires_at FROM auth_codes WHERE digest=?",
                             (_digest(code),)).fetchone()
            if row is None:
                return None
            stored_client, stored_redirect, stored_resource, scope, challenge, expiry = row
            if (now >= expiry or stored_client != client_id or stored_redirect != redirect_uri
                    or stored_resource != resource or not hmac.compare_digest(challenge, computed)):
                return None
            db.execute("DELETE FROM auth_codes WHERE digest=?", (_digest(code),))
            return scope
