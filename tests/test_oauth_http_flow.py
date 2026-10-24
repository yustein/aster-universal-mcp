"""Exercise the deployed gateway protocol over real loopback HTTPS.

Only owner actions (issuing a code and approving a client) touch local stores.
Registration, authorization, browser forms, PKCE exchange and MCP requests use
the actual gateway routes. This remains an integration test, not a real Claude
account acceptance test or proof of the external tunnel configuration.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_client_allowlist import update_allowlist
from approval_sessions import ApprovalSessions
from oauth_codes import pkce_s256
from pairing import PairingStore

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class BrowserPage(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.action = None
        self.fields = {}
        self.links = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs.get("action")
        elif tag == "input" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])


@dataclass
class Gateway:
    http: httpx.Client
    origin: str
    database: Path
    allowlist: Path
    allowed: Path
    tls_context: ssl.SSLContext

    def begin(self, *, scope="system:admin", resource=None):
        discovery = self.http.get("/.well-known/oauth-authorization-server")
        assert discovery.status_code == 200
        metadata = discovery.json()
        assert len(discovery.headers["x-aster-request-id"]) == 24
        assert metadata["issuer"].rstrip("/") == self.origin
        assert "system:admin" in metadata["scopes_supported"]
        assert "S256" in metadata["code_challenge_methods_supported"]
        for name in ("registration_endpoint", "authorization_endpoint", "token_endpoint"):
            assert metadata[name].startswith(self.origin + "/")
        registration = self.http.post(metadata["registration_endpoint"], json={
            "client_name": "Aster HTTP integration test",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "system:admin",
        })
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(24)
        response = self.http.get(metadata["authorization_endpoint"], params={
            "client_id": client_id, "response_type": "code",
            "redirect_uri": REDIRECT, "scope": scope,
            "state": state, "code_challenge": pkce_s256(verifier),
            "code_challenge_method": "S256",
            "resource": resource if resource is not None else self.origin + "/mcp",
        })
        return {"client_id": client_id, "verifier": verifier, "state": state,
                "token_endpoint": metadata["token_endpoint"], "response": response}

    def open_pairing(self, flow):
        response = flow["response"]
        assert response.status_code in (302, 303, 307), response.text
        location = response.headers["location"]
        assert location.startswith(self.origin + "/pair/authorize?")
        page = self.http.get(location)
        assert page.status_code == 200, page.text
        cookie = self.http.cookies.get("aster_pair_browser")
        assert cookie
        set_cookie = page.headers["set-cookie"].lower()
        assert "secure" in set_cookie and "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert "no-store" in page.headers["cache-control"]
        form = BrowserPage(page.text)
        assert form.action == "/pair/redeem"
        assert form.fields["request_id"] and form.fields["nonce"]
        flow["pairing_url"] = location
        return form

    def submit(self, form, **fields):
        return self.http.post(form.action, data={**form.fields, **fields},
                              headers={"Origin": self.origin})

    def finish(self, flow, form, code):
        paired = self.submit(form, pairing_code=code)
        assert paired.status_code == 303, paired.text
        page = self.http.get(paired.headers["location"])
        assert page.status_code == 200, page.text
        completion = BrowserPage(page.text)
        assert completion.action == "/pair/complete"
        assert completion.fields["nonce"] != form.fields["nonce"]
        finished = self.submit(completion)
        assert finished.status_code == 200, finished.text
        assert "no-store" in finished.headers["cache-control"]
        assert finished.headers["referrer-policy"] == "no-referrer"
        callbacks = BrowserPage(finished.text).links
        assert len(callbacks) == 1
        callback = urlsplit(callbacks[0])
        expected = urlsplit(REDIRECT)
        assert (callback.scheme, callback.netloc, callback.path) == (
            expected.scheme, expected.netloc, expected.path)
        parameters = parse_qs(callback.query)
        assert parameters["state"] == [flow["state"]]
        flow["authorization_code"] = parameters["code"][0]
        replay = self.submit(completion)
        assert replay.status_code == 403
        assert replay.json()["error"] == "invalid_completion"
        return flow

    def exchange(self, flow, **changes):
        return self.http.post(flow["token_endpoint"], data={
            "grant_type": "authorization_code", "client_id": flow["client_id"],
            "code": flow["authorization_code"], "redirect_uri": REDIRECT,
            "code_verifier": flow["verifier"], "resource": self.origin + "/mcp",
            **changes,
        })

    def token(self):
        flow = self.begin()
        form = self.open_pairing(flow)
        code = PairingStore(str(self.database)).issue(ttl_seconds=900)
        self.finish(flow, form, code)
        result = self.exchange(flow)
        assert result.status_code == 200, result.text
        return flow, result.json()["access_token"]

    def rpc(self, token, method, params, *, request_id=1):
        return self.http.post("/mcp", json={
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }, headers={"Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream"})


def _tls_files(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Aster loopback test")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([
                       x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = folder / "loopback-cert.pem", folder / "loopback-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    if os.name == "posix":
        key_path.chmod(0o600)
    return cert_path, key_path


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    database = tmp_path / "oauth.sqlite"
    allowlist = tmp_path / "admin-clients.json"
    allowlist.write_text("[]", encoding="utf-8-sig")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "http-acceptance-marker.txt").write_text("disposable test marker", encoding="utf-8")
    cert_path, key_path = _tls_files(tmp_path)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    origin = f"https://127.0.0.1:{port}"
    monkeypatch.setenv("ASTER_MCP_PUBLIC_ORIGIN", origin)
    monkeypatch.setenv("ASTER_MCP_OAUTH_DB", str(database))
    monkeypatch.setenv("ASTER_ALLOWED_ROOT", str(allowed))
    monkeypatch.setenv("ASTER_ADMIN_CLIENTS", str(allowlist))
    if os.name == "nt":
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "platforms" / "windows"))
        monkeypatch.setenv("ASTER_WINDOWS_PUBLIC_ORIGIN", origin)
        monkeypatch.setenv("ASTER_WINDOWS_OAUTH_DB", str(database))
        monkeypatch.setenv("ASTER_WINDOWS_ADMIN_CLIENTS", str(allowlist))
        from oauth_gateway import create_app
    else:
        from oauth_service import create_app
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, loop="asyncio", access_log=False,
        log_level="error", ssl_certfile=str(cert_path), ssl_keyfile=str(key_path),
        timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "The actual gateway did not start its HTTPS listener"
        context = ssl.create_default_context(cafile=str(cert_path))
        with httpx.Client(base_url=origin, verify=context, trust_env=False, timeout=5,
                          follow_redirects=False,
                          headers={"X-Forwarded-Proto": "https"}) as http:
            yield Gateway(http, origin, database, allowlist, allowed, context)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=3)
        sock.close()
        assert not thread.is_alive(), "Gateway test listener did not stop"


def _tool_result(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError", False) is False, result
    if "structuredContent" in result:
        return result["structuredContent"]
    return json.loads(next(item["text"] for item in result["content"] if item["type"] == "text"))


def test_live_https_wrong_code_recovery_pkce_exchange_and_mcp(gateway):
    g = gateway
    anonymous = g.http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Accept": "application/json, text/event-stream"})
    assert anonymous.status_code == 401
    assert "resource_metadata" in anonymous.headers["www-authenticate"]
    flow = g.begin()
    form = g.open_pairing(flow)
    # A harmless refresh must leave an unsubmitted form usable.
    refreshed = BrowserPage(g.http.get(flow["pairing_url"]).text)
    assert refreshed.fields["nonce"] == form.fields["nonce"]
    code = PairingStore(str(g.database)).issue(ttl_seconds=900)
    wrong = ("A" if code[0] != "A" else "B") + code[1:]
    rejected = g.submit(form, pairing_code=wrong)
    assert rejected.status_code == 403
    assert "invalid_or_expired_code" in rejected.text
    retry = BrowserPage(rejected.text)
    assert retry.action == "/pair/redeem"
    assert retry.fields["nonce"] != form.fields["nonce"]
    assert wrong not in rejected.text and code not in rejected.text
    replay = g.submit(form, pairing_code=code)
    assert replay.status_code == 403 and replay.json()["error"] == "invalid_session"
    g.finish(flow, retry, code.lower())
    bad_pkce = g.exchange(flow, code_verifier="wrong-verifier-" + "x" * 43)
    assert bad_pkce.status_code == 400
    assert bad_pkce.json()["error"] == "invalid_grant"
    exchanged = g.exchange(flow)
    assert exchanged.status_code == 200, exchanged.text
    grant = exchanged.json()
    assert grant["token_type"].lower() == "bearer"
    assert grant["scope"] == "system:admin" and grant["refresh_token"]
    duplicate = g.exchange(flow)
    assert duplicate.status_code == 400 and duplicate.json()["error"] == "invalid_grant"
    token = grant["access_token"]
    initialized = g.rpc(token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "Aster HTTPS acceptance", "version": "1"}})
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "Aster Universal MCP"
    listing = g.rpc(token, "tools/list", {}, request_id=2)
    assert listing.status_code == 200
    names = {tool["name"] for tool in listing.json()["result"]["tools"]}
    assert "list_directory" in names
    assert ("execute_command" if os.name == "nt" else "run_command") in names
    update_allowlist(g.allowlist, flow["client_id"], approve=True, db=g.database)
    directory = _tool_result(g.rpc(token, "tools/call", {
        "name": "list_directory", "arguments": {"path": str(g.allowed)}}, request_id=3))
    assert any(item == "http-acceptance-marker.txt" or
               isinstance(item, dict) and item.get("name") == "http-acceptance-marker.txt"
               for item in directory["entries"])


def test_http_pairing_rejects_wrong_origin_browser_and_nonce(gateway):
    g = gateway
    flow = g.begin()
    form = g.open_pairing(flow)
    code = PairingStore(str(g.database)).issue()
    data = {**form.fields, "pairing_code": code}
    hostile = g.http.post(form.action, data=data, headers={"Origin": "https://attacker.invalid"})
    assert hostile.status_code == 403 and hostile.json()["error"] == "invalid_origin"
    missing_origin = g.http.post(form.action, data=data)
    assert missing_origin.status_code == 403 and missing_origin.json()["error"] == "invalid_origin"
    malformed_nonce = g.submit(form, pairing_code=code, nonce="incorrect-nonce")
    assert malformed_nonce.status_code == 403 and malformed_nonce.json()["error"] == "invalid_session"
    with httpx.Client(base_url=g.origin, verify=g.tls_context, trust_env=False,
                      headers={"X-Forwarded-Proto": "https"}, timeout=5) as other_browser:
        bound = other_browser.get(flow["pairing_url"])
        assert bound.status_code == 403 and bound.json()["error"] == "pairing_session_already_bound"
        stolen_form = other_browser.post(form.action, data=data, headers={"Origin": g.origin})
        assert stolen_form.status_code == 403 and stolen_form.json()["error"] == "invalid_session"
    with sqlite3.connect(g.database) as db:
        assert db.execute("SELECT attempts FROM pairing").fetchone()[0] == 0
    # Rejections above must not consume the legitimate browser's form or code.
    g.finish(flow, form, code)
    assert g.exchange(flow).status_code == 200


def test_retry_forms_do_not_bypass_pairing_attempt_limit(gateway):
    g = gateway
    flow = g.begin()
    form = g.open_pairing(flow)
    code = PairingStore(str(g.database)).issue()
    wrong = ("A" if code[0] != "A" else "B") + code[1:]
    nonces = {form.fields["nonce"]}
    for _ in range(5):
        rejected = g.submit(form, pairing_code=wrong)
        assert rejected.status_code == 403 and "invalid_or_expired_code" in rejected.text
        form = BrowserPage(rejected.text)
        assert form.fields["nonce"] not in nonces
        nonces.add(form.fields["nonce"])
    rejected = g.submit(form, pairing_code=code)
    assert rejected.status_code == 403 and "invalid_or_expired_code" in rejected.text
    with sqlite3.connect(g.database) as db:
        assert db.execute("SELECT approved FROM approvals").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM access_tokens").fetchone()[0] == 0


def test_new_owner_code_does_not_revive_expired_authorization(gateway):
    g = gateway
    flow = g.begin()
    form = g.open_pairing(flow)
    with sqlite3.connect(g.database) as db:
        db.execute("UPDATE approvals SET expires_at=0")
    code = PairingStore(str(g.database)).issue(ttl_seconds=900)
    rejected = g.submit(form, pairing_code=code)
    assert rejected.status_code == 403 and rejected.json()["error"] == "invalid_request"
    assert g.http.get(flow["pairing_url"]).status_code == 404
    with sqlite3.connect(g.database) as db:
        assert db.execute("SELECT attempts FROM pairing").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM access_tokens").fetchone()[0] == 0


def test_http_authenticated_client_still_needs_operator_allowlist(gateway, caplog):
    g = gateway
    flow, token = g.token()
    name = "execute_command" if os.name == "nt" else "run_command"
    marker = g.allowed / "unapproved-command-must-not-run.txt"
    assert not marker.exists()
    caplog.set_level("ERROR")
    response = g.rpc(token, "tools/call", {"name": name, "arguments": {
        "command": [sys.executable, "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('unexpected execution')",
                    str(marker)],
        "cwd": str(g.allowed)}})
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    # The SDK deliberately returns a generic tool error to the client. Verify
    # the actual denial gate in server logs, without requiring detail leakage.
    assert "administrator client not authorized" in caplog.text
    assert not marker.exists()
    assert json.loads(g.allowlist.read_text(encoding="utf-8-sig")) == []


@pytest.mark.skipif(os.name != "nt", reason="Real Windows executor acceptance")
def test_windows_http_token_executes_only_after_operator_approval(gateway):
    g = gateway
    flow, token = g.token()
    update_allowlist(g.allowlist, flow["client_id"], approve=True, db=g.database)
    result = _tool_result(g.rpc(token, "tools/call", {"name": "execute_command", "arguments": {
        "command": [sys.executable, "-c", "print('HTTP-WINDOWS-EXECUTION-PASS')"],
        "cwd": str(g.allowed)}}))
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert result["stdout"].strip() == "HTTP-WINDOWS-EXECUTION-PASS"
    update_allowlist(g.allowlist, flow["client_id"], approve=False, db=g.database)
    denied = g.rpc(token, "tools/call", {"name": "execute_command", "arguments": {
        "command": [sys.executable, "-c", "print('must-not-execute')"], "cwd": str(g.allowed)}})
    assert denied.status_code == 200 and denied.json()["result"]["isError"] is True


@pytest.mark.parametrize("legacy_nonce_present", [True, False])
def test_existing_browser_session_migrates_without_reviving_consumed_nonce(tmp_path, legacy_nonce_present):
    database = tmp_path / "legacy.sqlite"
    browser, request_id = "legacy-browser-secret", "legacy-request"
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    legacy_nonce = hmac.new(browser.encode(), ("aster-complete:" + request_id).encode(),
                            hashlib.sha256).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE approval_sessions(request_id TEXT PRIMARY KEY, "
                   "browser_digest TEXT NOT NULL, nonce_digest TEXT, expires_at INTEGER NOT NULL)")
        db.execute("INSERT INTO approval_sessions VALUES (?,?,?,?)",
                   (request_id, digest(browser),
                    digest(legacy_nonce) if legacy_nonce_present else None,
                    int(time.time()) + 300))
    sessions = ApprovalSessions(str(database))
    initial = sessions.issue_completion(request_id, browser)
    if legacy_nonce_present:
        assert initial == legacy_nonce
    else:
        assert initial and initial != legacy_nonce
        assert not sessions.consume_nonce(request_id, browser, legacy_nonce)
    assert sessions.consume_nonce(request_id, browser, initial)
    fresh = sessions.issue_completion(request_id, browser)
    assert fresh and fresh != initial and fresh != legacy_nonce
    assert sessions.issue_completion(request_id, browser) == fresh
    assert not sessions.consume_nonce(request_id, browser, legacy_nonce)
    assert sessions.consume(request_id, browser, fresh)
    assert not sessions.consume(request_id, browser, fresh)


@pytest.mark.parametrize("changes", [
    {"scope": "system:read"},
    {"resource": "https://different-resource.invalid/mcp"},
])
def test_http_authorization_rejects_wrong_scope_or_resource(gateway, changes):
    flow = gateway.begin(**changes)
    response = flow["response"]
    if response.status_code in (302, 303):
        target = urlsplit(response.headers["location"])
        assert (target.scheme, target.netloc, target.path) == (
            "https", "claude.ai", "/api/mcp/auth_callback")
        assert "error" in parse_qs(target.query) and "code" not in parse_qs(target.query)
    else:
        assert response.status_code == 400, response.text
        assert response.json()["error"] in ("invalid_scope", "invalid_target", "invalid_request")
    with sqlite3.connect(gateway.database) as db:
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM access_tokens").fetchone()[0] == 0
