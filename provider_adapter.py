"""OAuth provider integrating owner-approved grants with the MCP SDK contract."""
import time
import hashlib
import sqlite3
from urllib.parse import quote
import json
from mcp.server.auth.provider import AuthorizationCode, AccessToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from client_registry import ClientRegistry
from oauth_codes import AuthorizationCodeStore
from token_store import TokenStore
from sdk_auth_codes import SDKAuthorizationCodes
from refresh_store import RefreshStore
from mcp.server.auth.provider import RefreshToken

class LocalOAuthProvider:
    def __init__(self, db_path: str, *, issuer: str, resource: str):
        self.clients = ClientRegistry(db_path)
        self.codes = AuthorizationCodeStore(db_path)
        self.sdk_codes = SDKAuthorizationCodes(db_path)
        self.tokens = TokenStore(db_path)
        self.refresh = RefreshStore(db_path)
        self.issuer = issuer.rstrip("/")
        self.resource = resource
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS client_metadata (client_id TEXT PRIMARY KEY, json TEXT NOT NULL)")

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("client ID required")
        redirects = [str(uri) for uri in (client_info.redirect_uris or [])]
        if not redirects:
            raise PermissionError("redirect URI required")
        existing = await self.get_client(client_info.client_id)
        if existing is not None:
            raise PermissionError("client ID already registered")
        with sqlite3.connect(self.clients.path) as db:
            if db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] >= 64:
                raise PermissionError("client registration capacity reached")
        if not all(self.clients.allowed(client_info.client_id, u) for u in redirects):
            self.clients.register_external(client_info.client_id, client_info.client_name or "MCP client", redirects)
        with sqlite3.connect(self.clients.path) as db:
            db.execute("INSERT OR REPLACE INTO client_metadata VALUES (?,?)",
                       (client_info.client_id, client_info.model_dump_json()))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with sqlite3.connect(self.clients.path) as db:
            row = db.execute("SELECT json FROM client_metadata WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            return None
        metadata = OAuthClientInformationFull.model_validate_json(row[0])
        redirects = [str(uri) for uri in (metadata.redirect_uris or [])]
        if not redirects or not all(self.clients.allowed(client_id, u) for u in redirects):
            return None
        return metadata

    async def authorize(self, client: OAuthClientInformationFull, params) -> str:
        """Create a pending request, never authorize it automatically.

        The returned URL is a local approval/status UI to be implemented.
        """
        from approval_store import ApprovalStore
        if not client.client_id:
            raise PermissionError("client ID required")
        redirect = str(params.redirect_uri)
        if not self.clients.allowed(client.client_id, redirect):
            raise PermissionError("redirect URI not registered")
        if params.resource != self.resource:
            raise PermissionError("MCP resource mismatch")
        requested = params.scopes or ["system:admin"]
        if requested != ["system:admin"]:
            raise PermissionError("only system:admin may be granted")
        if not params.code_challenge or len(params.code_challenge) != 43:
            raise PermissionError("PKCE S256 challenge required")
        approval = ApprovalStore(self.clients.path)
        request_id = approval.create(
            client_id=client.client_id, redirect_uri=redirect,
            resource=self.resource, scope="system:admin",
            code_challenge=params.code_challenge, state=params.state)
        return self.issuer + "/pair/authorize?request_id=" + quote(request_id, safe="")

    async def load_authorization_code(self, client, authorization_code: str):
        if not client.client_id:
            return None
        return self.sdk_codes.load(authorization_code, client.client_id)

    async def exchange_authorization_code(self, client, authorization_code):
        if not client.client_id or client.client_id != authorization_code.client_id:
            raise PermissionError("client mismatch")
        if authorization_code.resource != self.resource:
            raise PermissionError("resource mismatch")
        if not self.sdk_codes.consume(authorization_code.code, client.client_id):
            raise PermissionError("authorization code invalid or replayed")
        token = self.tokens.issue(client_id=client.client_id, resource=self.resource,
                                  scope=" ".join(authorization_code.scopes), ttl=900)
        refresh = self.refresh.issue(client.client_id, self.resource, " ".join(authorization_code.scopes))
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=900,
                          scope=" ".join(authorization_code.scopes), refresh_token=refresh)

    async def load_refresh_token(self, client, refresh_token: str):
        if not client.client_id:
            return None
        record = self.refresh.lookup(refresh_token, client.client_id)
        if record is None or record["resource"] != self.resource:
            return None
        with sqlite3.connect(self.clients.path) as db:
            revoked = db.execute("SELECT revoked FROM oauth_clients WHERE client_id=?", (client.client_id,)).fetchone()
        if revoked and revoked[0]:
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id,
                            scopes=record["scope"].split(), expires_at=record["expires_at"],
                            resource=self.resource, subject="paired-owner")

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if not client.client_id or refresh_token.client_id != client.client_id:
            raise PermissionError("client mismatch")
        if refresh_token.resource != self.resource:
            raise PermissionError("resource mismatch")
        requested = scopes or refresh_token.scopes
        if not set(requested).issubset(set(refresh_token.scopes)):
            raise PermissionError("scope escalation rejected")
        record = self.refresh.consume(refresh_token.token, client.client_id)
        if record is None or record["resource"] != self.resource:
            raise PermissionError("refresh token invalid or replayed")
        access = self.tokens.issue(client_id=client.client_id, resource=self.resource,
                                   scope=" ".join(requested), ttl=900)
        replacement = self.refresh.issue(client.client_id, self.resource, " ".join(requested))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=900,
                          scope=" ".join(requested), refresh_token=replacement)

    async def load_access_token(self, token: str) -> AccessToken | None:
        client_id = self.tokens.validate(token, resource=self.resource, scope="system:admin")
        if client_id is None:
            return None
        with sqlite3.connect(self.clients.path) as db:
            revoked = db.execute("SELECT revoked FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        if revoked and revoked[0]:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.tokens.path) as db:
            row = db.execute("SELECT expires_at FROM access_tokens WHERE digest=?", (digest,)).fetchone()
        if row is None or row[0] <= int(time.time()):
            return None
        return AccessToken(token=token, client_id=client_id, scopes=["system:admin"],
                           expires_at=row[0], resource=self.resource)

    async def revoke_token(self, token) -> None:
        """Invalidate the presented credential in its corresponding store.

        This release keeps the public revocation endpoint disabled. The stores
        do not associate access/refresh token families, so this method does not
        claim the SDK's recommended associated-token revocation behavior.
        Local operator allowlist removal blocks every token of that client.
        """
        if isinstance(token, RefreshToken):
            self.refresh.revoke(token.token)
        else:
            self.tokens.revoke(token.token)
