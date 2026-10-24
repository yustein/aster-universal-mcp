"""Provider revocation regression checks without enabling a public revoke route."""
import asyncio
from pathlib import Path
import sys

from mcp.shared.auth import OAuthClientInformationFull

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provider_adapter import LocalOAuthProvider

ORIGIN = "https://refresh-test.invalid"
RESOURCE = ORIGIN + "/mcp"


def _client(provider, client_id):
    client = OAuthClientInformationFull(
        client_id=client_id, client_name="Isolated revocation test",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none",
    )
    asyncio.run(provider.register_client(client))
    return client


def test_provider_refresh_revocation_invalidates_the_refresh_credential(tmp_path):
    provider = LocalOAuthProvider(str(tmp_path / "oauth.sqlite"), issuer=ORIGIN, resource=RESOURCE)
    owner = _client(provider, "owner-client")
    other = _client(provider, "other-client")
    refresh = provider.refresh.issue(owner.client_id, RESOURCE, "system:admin")
    unrelated = provider.refresh.issue(other.client_id, RESOURCE, "system:admin")
    loaded = asyncio.run(provider.load_refresh_token(owner, refresh))
    assert loaded is not None
    asyncio.run(provider.revoke_token(loaded))
    assert asyncio.run(provider.load_refresh_token(owner, refresh)) is None
    assert provider.refresh.consume(refresh, owner.client_id) is None
    assert asyncio.run(provider.load_refresh_token(other, unrelated)) is not None
    # Revocation is idempotent and does not rely on a still-live lookup.
    asyncio.run(provider.revoke_token(loaded))
    assert provider.refresh.lookup(refresh, owner.client_id) is None


def test_provider_access_revocation_invalidates_only_the_target_access_credential(tmp_path):
    provider = LocalOAuthProvider(str(tmp_path / "oauth.sqlite"), issuer=ORIGIN, resource=RESOURCE)
    owner = _client(provider, "owner-client")
    first = provider.tokens.issue(client_id=owner.client_id, resource=RESOURCE, scope="system:admin")
    second = provider.tokens.issue(client_id=owner.client_id, resource=RESOURCE, scope="system:admin")
    loaded = asyncio.run(provider.load_access_token(first))
    assert loaded is not None
    asyncio.run(provider.revoke_token(loaded))
    assert asyncio.run(provider.load_access_token(first)) is None
    assert asyncio.run(provider.load_access_token(second)) is not None
    asyncio.run(provider.revoke_token(loaded))
    assert asyncio.run(provider.load_access_token(first)) is None
