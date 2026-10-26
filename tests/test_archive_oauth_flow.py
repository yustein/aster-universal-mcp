"""Archive pairing uses its own origin, UI and protected read grant end to end.

The real OAuth routes run in HTTPS ASGI behind the same trusted-proxy boundary.
Only the corpus backend is synthetic; SQLite registration, browser sessions,
one-use pairing/completion, PKCE exchange and authenticated MCP are real.
"""
from __future__ import annotations

import asyncio
from html import escape
import os
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from aster_archive.access import ArchiveScope
from aster_archive.service import _archive_onboarding, create_app
from oauth_codes import pkce_s256
from pairing import PairingStore
from test_archive_tools import PID, ScopedStore, grant, write_acl
from test_oauth_http_flow import BrowserPage, _tool_result


ORIGIN = "https://archive-pairing.invalid"
CONTROL_ORIGIN = "https://separate-control.invalid"
CALLBACK = "https://chatgpt.com/connector_platform/oauth_redirect"


@pytest.mark.parametrize("global_origin", [None, CONTROL_ORIGIN])
def test_archive_https_pairing_pkce_and_scoped_read_ignore_control_origin(
        tmp_path, monkeypatch, global_origin):
    if global_origin is None:
        monkeypatch.delenv("ASTER_MCP_PUBLIC_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("ASTER_MCP_PUBLIC_ORIGIN", global_origin)
    database = tmp_path / "archive-oauth.sqlite3"
    acl = tmp_path / "archive-clients.json"
    write_acl(acl, {})
    original_acl = acl.read_bytes()
    store = ScopedStore()
    app = create_app(backend=store, environment={
        "ASTER_ARCHIVE_PUBLIC_ORIGIN": ORIGIN,
        "ASTER_ARCHIVE_OAUTH_DB": str(database),
        "ASTER_ARCHIVE_CLIENTS": str(acl),
        "ASTER_ARCHIVE_TRUSTED_PROXY_HOST": "127.0.0.1",
    })
    assert os.environ.get("ASTER_MCP_PUBLIC_ORIGIN") == global_origin

    async def flow():
        inner = app.app
        headers = {"X-Forwarded-Proto": "https",
                   "Accept": "application/json, text/event-stream"}
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 10000)),
                base_url=ORIGIN, headers=headers, follow_redirects=False,
            ) as http:
                discovery = await http.get("/.well-known/oauth-authorization-server")
                assert discovery.status_code == 200
                metadata = discovery.json()
                assert metadata["issuer"].rstrip("/") == ORIGIN
                assert all(metadata[k].startswith(ORIGIN + "/") for k in (
                    "registration_endpoint", "authorization_endpoint", "token_endpoint"))
                registered = await http.post(metadata["registration_endpoint"], json={
                    "client_name": "Synthetic archive client", "redirect_uris": [CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "token_endpoint_auth_method": "none",
                    "scope": "system:admin",
                })
                assert registered.status_code == 201
                client_id = registered.json()["client_id"]
                verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
                started = await http.get(metadata["authorization_endpoint"], params={
                    "client_id": client_id, "response_type": "code", "redirect_uri": CALLBACK,
                    "scope": "system:admin", "resource": ORIGIN + "/mcp", "state": state,
                    "code_challenge": pkce_s256(verifier), "code_challenge_method": "S256",
                })
                assert started.status_code in (302, 303, 307)
                page = await http.get(started.headers["location"])
                assert page.status_code == 200
                cookie = page.headers["set-cookie"].lower()
                assert all(flag in cookie for flag in ("secure", "httponly", "samesite=lax"))
                assert http.cookies.get("aster_pair_browser")
                assert "data-archive-status='approval_required'" in page.text
                assert client_id in page.text and CALLBACK in page.text
                form = BrowserPage(page.text)
                assert form.action == "/pair/redeem" and form.fields["nonce"]
                code = PairingStore(str(database)).issue(ttl_seconds=300)
                fields = {**form.fields, "pairing_code": code}

                # A separate Control origin cannot redeem an archive code, even
                # when it is the process-wide environment setting.
                rejected = await http.post(form.action, data=fields,
                                           headers={"Origin": CONTROL_ORIGIN})
                assert rejected.status_code == 403 and rejected.json()["error"] == "invalid_origin"
                no_origin = await http.post(form.action, data=fields)
                assert no_origin.status_code == 403 and no_origin.json()["error"] == "invalid_origin"
                bad_nonce = await http.post(form.action, data={**fields, "nonce": "wrong"},
                                            headers={"Origin": ORIGIN})
                assert bad_nonce.status_code == 403 and bad_nonce.json()["error"] == "invalid_session"
                redeemed = await http.post(form.action, data=fields, headers={"Origin": ORIGIN})
                assert redeemed.status_code == 303
                approved = await http.get(redeemed.headers["location"])
                completion = BrowserPage(approved.text)
                assert completion.action == "/pair/complete"
                assert completion.fields["nonce"] != form.fields["nonce"]
                rejected = await http.post(completion.action, data=completion.fields,
                                           headers={"Origin": CONTROL_ORIGIN})
                assert rejected.status_code == 403 and rejected.json()["error"] == "invalid_origin"
                finished = await http.post(completion.action, data=completion.fields,
                                          headers={"Origin": ORIGIN})
                assert finished.status_code == 200
                for response in (page, approved, finished):
                    assert "data-archive-status='approval_required'" in response.text
                    assert "archive_coverage" in response.text
                    assert "no-store" in response.headers["cache-control"]
                    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
                    assert not any(text in response.text for text in (
                        "operator-linux.py", "Administrator approval", "run_command",
                        "execute_command", "connection_status", "data-admin-status"))
                assert acl.read_bytes() == original_acl and store.calls == []
                replay = await http.post(completion.action, data=completion.fields,
                                         headers={"Origin": ORIGIN})
                assert replay.status_code == 403 and replay.json()["error"] == "invalid_completion"
                callbacks = BrowserPage(finished.text).links
                assert len(callbacks) == 1 and callbacks[0].startswith(CALLBACK + "?")
                callback = parse_qs(urlsplit(callbacks[0]).query)
                assert callback["state"] == [state]
                exchange = {
                    "grant_type": "authorization_code", "client_id": client_id,
                    "code": callback["code"][0], "redirect_uri": CALLBACK,
                    "code_verifier": verifier, "resource": ORIGIN + "/mcp",
                }
                wrong_pkce = await http.post(metadata["token_endpoint"],
                    data={**exchange, "code_verifier": "wrong-" + "x" * 50})
                assert wrong_pkce.status_code == 400
                tokens = await http.post(metadata["token_endpoint"], data=exchange)
                assert tokens.status_code == 200 and tokens.json()["scope"] == "system:admin"
                duplicate = await http.post(metadata["token_endpoint"], data=exchange)
                assert duplicate.status_code == 400
                authenticated = {"Authorization": "Bearer " + tokens.json()["access_token"]}
                passage = "/archive/passages/" + PID
                request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "fetch", "arguments": {"id": PID}}}
                assert (await http.get(passage, headers=authenticated)).status_code == 403
                denied = await http.post("/mcp", json=request, headers=authenticated)
                assert denied.json()["result"]["isError"] is True and store.calls == []

                # The local ACL is a distinct authorization step. A grant for
                # other sources remains unable to reveal the synthetic passage.
                write_acl(acl, {client_id: grant(sources=["work"], projects=["fleet"])})
                assert (await http.get(passage, headers=authenticated)).status_code == 404
                write_acl(acl, {client_id: grant(sources=["personal"], projects=["coordinator"])})
                initialized = await http.post("/mcp", headers=authenticated, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "Archive pairing test", "version": "1"}}})
                assert initialized.status_code == 200
                assert initialized.json()["result"]["serverInfo"]["name"] == "Aster Conversation Archive"
                result = _tool_result(await http.post("/mcp", json=request, headers=authenticated))
                assert result["id"] == PID and result["url"] == ORIGIN + passage
                exact = await http.get(passage, headers=authenticated)
                assert exact.status_code == 200 and exact.json()["text"] == result["text"]
                assert store.calls[-1][1] == ArchiveScope(
                    client_id, "owner", ("personal",), ("coordinator",))
                write_acl(acl, {})
                assert (await http.get(passage, headers=authenticated)).status_code == 403
    asyncio.run(asyncio.wait_for(flow(), 20))
    assert os.environ.get("ASTER_MCP_PUBLIC_ORIGIN") == global_origin


def test_archive_onboarding_reloads_grants_and_escapes_identity(tmp_path):
    acl = tmp_path / "clients.json"
    client = "synthetic'<script>alert(1)</script>"
    callback = 'https://example.invalid/callback?x=<img src=x onerror=alert(1)>'
    record = {"client_id": client, "redirect_uri": callback}
    write_acl(acl, {})
    page = _archive_onboarding(record, acl)
    assert "data-archive-status='approval_required'" in page
    assert escape(client) in page and escape(callback) in page
    assert "<script>" not in page and "<img" not in page
    write_acl(acl, {client: grant()})
    before = acl.read_bytes()
    assert "data-archive-status='approved'" in _archive_onboarding(record, acl)
    assert acl.read_bytes() == before
    write_acl(acl, {client: grant(sources=[])})
    assert "data-archive-status='empty_scope'" in _archive_onboarding(record, acl)
    acl.write_text("invalid grants")
    assert "data-archive-status='authorization_state_unavailable'" in _archive_onboarding(record, acl)
    assert acl.read_text() == "invalid grants"
    acl.unlink()
    assert "data-archive-status='authorization_state_unavailable'" in _archive_onboarding(record, acl)
    assert not acl.exists()
