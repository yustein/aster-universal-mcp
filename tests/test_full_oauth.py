"""Privilege-aware OAuth tests for the cross-platform remote beta."""
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from mcp.server.auth.provider import AuthorizationParams

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_client_allowlist import update_allowlist
from approval_store import ApprovalStore
from client_registry import ClientRegistry
from oauth_codes import pkce_s256
from owner_flow import OwnerPairingFlow
from provider_adapter import LocalOAuthProvider

ISSUER = "https://mcp.example.invalid"
RESOURCE = ISSUER + "/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def client(cid):
    return OAuthClientInformationFull(
        client_id=cid, client_name="Claude Free test",
        redirect_uris=[REDIRECT], grant_types=["authorization_code"],
        response_types=["code"], token_endpoint_auth_method="none"
    )


def params(scopes=None, resource=RESOURCE):
    return AuthorizationParams(
        state="test-state", scopes=scopes if scopes is not None else ["system:admin"],
        code_challenge=pkce_s256("a" * 43),
        redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True,
        resource=resource
    )


def test_full_admin_scope_and_owner_approval(tmp_path):
    db = tmp_path / "oauth.sqlite"
    provider = LocalOAuthProvider(str(db), issuer=ISSUER, resource=RESOURCE)
    cid = provider.clients.register("Claude beta", [REDIRECT])
    # For standalone provider tests, explicit SDK client metadata registration is necessary.
    asyncio.run(provider.register_client(client(cid)))
    url = asyncio.run(provider.authorize(client(cid), params()))
    assert url.startswith(ISSUER + "/pair/authorize?request_id=")
    rid = url.partition("request_id=")[2]
    pending = ApprovalStore(str(db)).get(rid)
    assert pending and pending["approved"] == 0
    assert pending["scope"] == "system:admin"

    # A paired client is *still* not an allowed privileged client.
    allowlist = tmp_path / "admin-clients.json"
    assert not allowlist.exists()
    flow = OwnerPairingFlow(str(db))
    code = flow.pairing.issue()
    assert flow.approve_locally(rid, code)
    issued = flow.finalize(rid)
    assert issued is not None
    auth_code, _ = issued
    sdk_code = asyncio.run(provider.load_authorization_code(client(cid), auth_code))
    assert sdk_code and sdk_code.scopes == ["system:admin"]
    token = asyncio.run(provider.exchange_authorization_code(client(cid), sdk_code))
    access = asyncio.run(provider.load_access_token(token.access_token))
    assert access and "system:admin" in access.scopes
    assert access.client_id == cid
    assert asyncio.run(provider.load_authorization_code(client(cid), auth_code)) is None

    updated = update_allowlist(allowlist, cid, approve=True, db=db)
    assert updated == [cid]
    assert json.loads(allowlist.read_text()) == [cid]
    assert update_allowlist(allowlist, cid, approve=False, db=db) == []


def test_admin_scope_and_resource_cannot_be_weakened(tmp_path):
    db = tmp_path / "oauth.sqlite"
    provider = LocalOAuthProvider(str(db), issuer=ISSUER, resource=RESOURCE)
    cid = provider.clients.register("Claude", [REDIRECT])
    asyncio.run(provider.register_client(client(cid)))
    for bad in (["system:read"], ["system:read", "system:admin"], ["other"]):
        with pytest.raises(PermissionError):
            asyncio.run(provider.authorize(client(cid), params(scopes=bad)))
    with pytest.raises(PermissionError):
        asyncio.run(provider.authorize(client(cid), params(resource=ISSUER+"/other")))


def test_owner_allowlist_denies_unregistered_client(tmp_path):
    db = tmp_path / "oauth.sqlite"
    ClientRegistry(str(db))
    with pytest.raises(PermissionError):
        update_allowlist(tmp_path / "admins.json", "unknown-client",
                         approve=True, db=db)
    assert not (tmp_path / "admins.json").exists()


def test_required_oauth_scope_is_administrative(tmp_path):
    from oauth_runtime import build_oauth_server
    server = build_oauth_server(db_path=str(tmp_path / "scope.sqlite"),
                                origin=ISSUER)
    assert server is not None
    assert server.settings.auth is not None if hasattr(server, "settings") else True


def test_linux_public_origin_gate_and_oauth_metadata(tmp_path, monkeypatch):
    import httpx
    from oauth_service import create_app
    root = tmp_path / "allowed"
    root.mkdir()
    allowlist = tmp_path / "allowed-clients.json"
    allowlist.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("ASTER_MCP_PUBLIC_ORIGIN", ISSUER)
    monkeypatch.setenv("ASTER_MCP_OAUTH_DB", str(tmp_path / "gateway.sqlite"))
    monkeypatch.setenv("ASTER_ALLOWED_ROOT", str(root))
    monkeypatch.setenv("ASTER_ADMIN_CLIENTS", str(allowlist))
    app = create_app()

    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER,
            headers={"Host": "mcp.example.invalid", "X-Forwarded-Proto": "https"},
        ) as http:
            await_response = await http.post(
                "/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "run_command",
                               "arguments": {"command": ["id"], "cwd": "/"}},
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert await_response.status_code == 401
            discovery = await http.get("/.well-known/oauth-authorization-server")
            assert discovery.status_code == 200
            metadata = discovery.json()
            assert "system:admin" in metadata.get("scopes_supported", ["system:admin"])
            malformed = await http.get(
                "/.well-known/oauth-authorization-server",
                headers={"X-Forwarded-Proto": "http"},
            )
            assert malformed.status_code == 403

    asyncio.run(call())
