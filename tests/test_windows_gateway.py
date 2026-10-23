"""Windows full OAuth gateway: proxy boundary and administrative tool checks."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows gateway requires Win32")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_windows_oauth_requires_trusted_https_proxy_and_bearer(tmp_path, monkeypatch):
    import httpx
    win = Path(__file__).resolve().parents[1] / "platforms" / "windows"
    sys.path.insert(0, str(win))
    from oauth_gateway import create_app
    origin = "https://mcp.example.invalid"
    db = tmp_path / "windows-oauth.sqlite"
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("ASTER_WINDOWS_PUBLIC_ORIGIN", origin)
    monkeypatch.setenv("ASTER_WINDOWS_OAUTH_DB", str(db))
    monkeypatch.setenv("ASTER_WINDOWS_ADMIN_CLIENTS", str(allowlist))
    app = create_app()
    assert db.is_file()

    async def verify():
        headers = {
            "Host": "mcp.example.invalid",
            "X-Forwarded-Proto": "https",
            "Accept": "application/json, text/event-stream",
        }
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(transport=transport, base_url=origin) as http:
                payload = {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "execute_command",
                               "arguments": {"command": ["whoami"], "cwd": str(tmp_path)}}
                }
                denied = await http.post("/mcp", json=payload, headers=headers)
                assert denied.status_code == 401
                untrusted = await http.post(
                    "/mcp", json=payload,
                    headers={**headers, "X-Forwarded-Proto": "http"}
                )
                assert untrusted.status_code == 403
                metadata = await http.get(
                    "/.well-known/oauth-authorization-server", headers=headers
                )
                assert metadata.status_code == 200
                assert "system:admin" in metadata.json().get("scopes_supported", [])

                from client_registry import ClientRegistry
                from token_store import TokenStore
                cid = ClientRegistry(str(db)).register(
                    "Owner approved test", ["https://claude.ai/api/mcp/auth_callback"]
                )
                token = TokenStore(str(db)).issue(
                    client_id=cid, resource=origin + "/mcp",
                    scope="system:admin"
                )
                authenticated = {**headers, "Authorization": "Bearer " + token}
                before_approval = await http.post(
                    "/mcp", json=payload, headers=authenticated
                )
                assert before_approval.status_code in (200, 403)
                assert before_approval.status_code == 403 or (
                    "error" in before_approval.text.lower()
                )
                allowlist.write_text(json.dumps([cid]), encoding="utf-8")
                allowed = await http.post(
                    "/mcp", json=payload, headers=authenticated
                )
                assert allowed.status_code == 200, allowed.text[:500]
                assert "completed" in allowed.text.lower()
    asyncio.run(asyncio.wait_for(verify(), timeout=45))
