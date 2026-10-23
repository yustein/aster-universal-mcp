"""SDK v2 AuthorizationCode bridge with exact client/redirect/resource checks.

PKCE verifier verification remains in the OAuth SDK token endpoint.
"""
import hashlib
import sqlite3
import time
from mcp.server.auth.provider import AuthorizationCode

class SDKAuthorizationCodes:
    def __init__(self, db_path: str):
        self.path = db_path

    def load(self, code: str, client_id: str) -> AuthorizationCode | None:
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT client_id,redirect_uri,resource,scope,challenge,expires_at FROM auth_codes WHERE digest=?",
                (digest,)).fetchone()
        if row is None:
            return None
        stored_client, redirect, resource, scope, challenge, expiry = row
        if stored_client != client_id or expiry <= time.time():
            return None
        return AuthorizationCode(
            code=code, client_id=stored_client, redirect_uri=redirect,
            redirect_uri_provided_explicitly=True,
            resource=resource, scopes=scope.split(),
            code_challenge=challenge, expires_at=float(expiry),
            subject="paired-owner")

    def consume(self, code: str, client_id: str) -> bool:
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        with sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE") as db:
            row = db.execute(
                "DELETE FROM auth_codes WHERE digest=? AND client_id=? AND expires_at>? RETURNING digest",
                (digest, client_id, int(time.time()))).fetchone()
            return row is not None
