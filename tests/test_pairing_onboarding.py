"""User-visible onboarding remains separate from OAuth and permission grants."""
import os
from html import escape
from html.parser import HTMLParser

import pytest

from test_oauth_http_flow import gateway, BrowserPage
from admin_client_allowlist import update_allowlist
from approval_http import administrator_onboarding
from pairing import PairingStore


def finish_page(g, flow, form):
    code = PairingStore(str(g.database)).issue(ttl_seconds=900)
    paired = g.submit(form, pairing_code=code)
    assert paired.status_code == 303
    middle = g.http.get(paired.headers["location"])
    assert middle.status_code == 200
    completed = g.submit(BrowserPage(middle.text))
    assert completed.status_code == 200
    return middle, completed


def test_real_tls_pairing_explains_exact_local_grant_without_creating_one(gateway):
    g = gateway
    before = g.allowlist.read_bytes()
    flow = g.begin()
    form = g.open_pairing(flow)
    first = g.http.get(flow["pairing_url"])
    assert "Requested OAuth scope" in first.text
    assert "data-admin-status='approval_required'" in first.text
    assert flow["client_id"] in first.text
    assert "https://claude.ai/api/mcp/auth_callback" in first.text
    # Reopening refreshes the browser's one-use form nonce.
    middle, completed = finish_page(g, flow, BrowserPage(first.text))
    for response in (first, middle, completed):
        assert "Administrator approval required" in response.text
        assert "connection_status" in response.text
        assert "exit_code: 0" in response.text
        assert "does not verify command execution" in response.text
        assert "no-store" in response.headers["cache-control"]
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert "-Action clients" in response.text if os.name == "nt" else "operator-linux.py" in response.text
    assert "OAuth pairing complete" in completed.text
    assert len(BrowserPage(completed.text).links) == 1
    assert completed.headers["referrer-policy"] == "no-referrer"
    assert g.allowlist.read_bytes() == before


def test_pairing_page_reports_only_current_clients_fresh_grant(gateway):
    g = gateway
    flow = g.begin()
    g.open_pairing(flow)
    update_allowlist(g.allowlist, flow["client_id"], approve=True, db=g.database)
    page = g.http.get(flow["pairing_url"])
    assert "data-admin-status='approved'" in page.text
    assert "Verify an actual command" in page.text
    update_allowlist(g.allowlist, flow["client_id"], approve=False, db=g.database)
    page = g.http.get(flow["pairing_url"])
    assert "data-admin-status='approval_required'" in page.text


@pytest.mark.parametrize("bad", [b'{"not":"an allowlist"}', b'not json', None])
def test_pairing_does_not_mislabel_or_repair_unavailable_authorization_state(gateway, bad):
    g = gateway
    flow = g.begin()
    if bad is None:
        g.allowlist.unlink()
    else:
        g.allowlist.write_bytes(bad)
    g.open_pairing(flow)
    page = g.http.get(flow["pairing_url"])
    assert "data-admin-status='authorization_state_unavailable'" in page.text
    assert "Administrator permission could not be checked" in page.text
    assert "Do not recreate it" in page.text
    if bad is None:
        assert not g.allowlist.exists()
    else:
        assert g.allowlist.read_bytes() == bad


@pytest.mark.parametrize("windows", [False, True])
def test_metadata_cannot_create_markup_and_operator_arguments_are_quoted(tmp_path, monkeypatch, windows):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]")
    monkeypatch.setenv("ASTER_ADMIN_CLIENTS", str(allowlist))
    if windows:
        monkeypatch.setenv("ASTER_WINDOWS_ADMIN_CLIENTS", str(allowlist))
    else:
        monkeypatch.delenv("ASTER_WINDOWS_ADMIN_CLIENTS", raising=False)
        if os.name == "nt":
            pytest.skip("POSIX command rendering is checked on the native Linux runner")
    client = "test'; <script>alert(1)</script> $(touch x)"
    callback = 'https://example.test/callback?x=<img src=x onerror=alert(1)>'
    page = administrator_onboarding({"client_id": client, "redirect_uri": callback})
    assert "<script>" not in page and "<img" not in page
    assert escape(client) in page and escape(callback) in page
    plain = []
    class Text(HTMLParser):
        def handle_data(self, data):
            plain.append(data)
    Text(convert_charrefs=True).feed(page)
    rendered = "".join(plain)
    if windows:
        assert "-ClientId '" + client.replace("'", "''") + "'" in rendered
    else:
        import shlex
        assert "approve " + shlex.quote(client) in rendered
