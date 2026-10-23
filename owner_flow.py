"""Local orchestration of owner-approved authorization.

No network endpoints. In production the redeem-and-approve operation MUST
be reachable only from a separately authenticated owner context.
"""
from approval_store import ApprovalStore
from client_registry import ClientRegistry
from oauth_codes import AuthorizationCodeStore
from pairing import PairingStore
from token_store import TokenStore

class OwnerPairingFlow:
    def __init__(self, db_path: str):
        self.clients = ClientRegistry(db_path)
        self.approvals = ApprovalStore(db_path)
        self.pairing = PairingStore(db_path)
        self.codes = AuthorizationCodeStore(db_path)
        self.tokens = TokenStore(db_path)

    def request(self, *, client_id: str, redirect_uri: str, resource: str,
                scope: str, challenge: str, state: str | None = None) -> str:
        if not self.clients.allowed(client_id, redirect_uri):
            raise PermissionError("client or redirect URI not registered")
        if scope != "system:admin":
            raise PermissionError("only explicitly approved admin scope is enabled")
        return self.approvals.create(
            client_id=client_id, redirect_uri=redirect_uri, resource=resource,
            scope=scope, code_challenge=challenge, state=state)

    def approve_locally(self, request_id: str, pairing_code: str) -> bool:
        """Call only after a separate local owner-presence check.

        A pairing code alone must not become a remotely callable admin tool.
        """
        if self.approvals.get(request_id) is None:
            return False
        if not self.pairing.consume(pairing_code):
            return False
        return self.approvals.approve(request_id)

    def finalize(self, request_id: str) -> tuple[str, dict] | None:
        approved = self.approvals.consume_approved(request_id)
        if approved is None:
            return None
        code = self.codes.issue_approved(
            client_id=approved["client_id"],
            redirect_uri=approved["redirect_uri"],
            resource=approved["resource"],
            scope=approved["scope"],
            code_challenge=approved["code_challenge"])
        return code, approved

    def exchange(self, *, code: str, client_id: str, redirect_uri: str,
                 resource: str, verifier: str) -> str | None:
        if not self.clients.allowed(client_id, redirect_uri):
            return None
        scope = self.codes.consume(code, client_id=client_id,
                                   redirect_uri=redirect_uri, resource=resource,
                                   verifier=verifier)
        if scope is None:
            return None
        return self.tokens.issue(client_id=client_id, resource=resource, scope=scope)
