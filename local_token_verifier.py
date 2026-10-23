"""MCP SDK v2 TokenVerifier backed by our local token store.

Read-only M1 resource scope. No OAuth authorization server endpoints yet.
"""
from mcp.server.auth.provider import AccessToken
from token_store import TokenStore
import hashlib
import sqlite3
import time

class LocalTokenVerifier:
    def __init__(self, db_path: str, resource: str, required_scope: str = "system:read"):
        if not resource.startswith("https://") or not resource.endswith("/mcp"):
            raise ValueError("canonical HTTPS MCP resource required")
        self.store = TokenStore(db_path)
        self.resource = resource
        self.required_scope = required_scope

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        # TokenStore persists hashes only. Do not return plaintext token to logs.
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.store.path) as db:
            row = db.execute(
                "SELECT client_id,resource,scope,expires_at,revoked FROM access_tokens WHERE digest=?",
                (digest,)).fetchone()
        if row is None:
            return None
        client_id, resource, scopes, expiry, revoked = row
        if revoked or int(time.time()) >= expiry or resource != self.resource:
            return None
        if self.required_scope not in scopes.split():
            return None
        return AccessToken(token=token, client_id=client_id,
                           scopes=scopes.split(), expires_at=expiry,
                           resource=resource, subject="paired-owner")
