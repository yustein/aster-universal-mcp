"""Browser-bound owner pairing and one-use OAuth completion.

Pairing codes are issued only through the local operator channel, then redeemed
on the canonical HTTPS origin using a bound browser cookie and one-use form nonce.
"""
from html import escape
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
import time
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from approval_store import ApprovalStore
from oauth_codes import AuthorizationCodeStore
from approval_sessions import ApprovalSessions
from pairing import PairingStore
import os
from pathlib import Path
import shlex


def onboarding_head(title):
    return ("<head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>" + escape(title) + "</title><style>"
            "body{font:16px/1.55 system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#17212b}"
            "section{border-top:1px solid #ccd3dc;margin-top:2rem;padding-top:1rem}"
            "pre,code,dd{overflow-wrap:anywhere}pre{white-space:pre-wrap;background:#f0f3f6;padding:.8rem;border-radius:.4rem}"
            "dd{margin:0 0 .7rem}dt{font-weight:600}input,button{font:inherit;padding:.65rem;max-width:100%;box-sizing:border-box}"
            "button{margin:.6rem 0;cursor:pointer}h1,h2{line-height:1.2}"
            "</style></head>")


def administrator_onboarding(record):
    """Explain this client's local grant without changing OAuth or permission state."""
    from mcp.server.mcpserver.exceptions import ToolError
    from tool_access import read_admin_allowlist

    windows = os.name == "nt" or bool(os.environ.get("ASTER_WINDOWS_ADMIN_CLIENTS"))
    client_id = record["client_id"]
    if windows:
        allowlist = os.environ.get("ASTER_WINDOWS_ADMIN_CLIENTS")
        terminal = "an elevated PowerShell terminal in your extracted Aster folder"
        prefix = ("powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
                  ".\\scripts\\manage-client-windows.ps1 -Action ")
        inspect_command = prefix + "clients"
        approve_command = prefix + "approve -ClientId '" + client_id.replace("'", "''") + "'"
        command_tool = "execute_command"
        sample = r'{"command":["C:\\Windows\\System32\\hostname.exe"]}'
    else:
        allowlist = os.environ.get("ASTER_ADMIN_CLIENTS", "/var/lib/aster-universal-mcp/admin-client-allowlist.json")
        state = str(Path(allowlist).parent)
        prefix = ("sudo /usr/bin/python3 " + shlex.quote(str(Path(state) / "operator-linux.py"))
                  + " --state " + shlex.quote(state) + " ")
        terminal = "a local administrator terminal on your Aster server"
        inspect_command = prefix + "clients"
        approve_command = prefix + "approve " + shlex.quote(client_id)
        command_tool = "run_command"
        sample = '{"command":["/bin/hostname"]}'
    try:
        if not allowlist:
            raise ToolError("AUTHORIZATION_STATE_UNAVAILABLE")
        approved = client_id in read_admin_allowlist(allowlist)
    except ToolError:
        status = "authorization_state_unavailable"
        heading = "Administrator permission could not be checked"
        detail = ("Ask the local operator to inspect Aster's protected authorization state. "
                  "Do not recreate it or assume this client is approved.")
    else:
        status = "approved" if approved else "approval_required"
        heading = "Administrator permission granted" if approved else "Administrator approval required"
        detail = ("This client has a local administrator grant. Verify an actual command before declaring setup complete."
                  if approved else "OAuth pairing connects your account. Commands, jobs and file transfers also require a separate local administrator grant.")
    return ("<section aria-labelledby='administrator-approval' data-admin-status='" + status + "'>"
            "<h2 id='administrator-approval'>" + heading + "</h2><p>" + detail + "</p>"
            "<p>Finish the OAuth connection and return to your client promptly. "
            "You can complete the local approval step afterwards; do not wait for it on this expiring page.</p>"
            "<p>Match this exact client ID and callback in " + terminal + ":</p>"
            "<dl><dt>Client ID</dt><dd><code>" + escape(client_id) + "</code></dd>"
            "<dt>Callback</dt><dd><code>" + escape(record["redirect_uri"]) + "</code></dd></dl>"
            "<pre>" + escape(inspect_command) + "</pre>"
            "<p>If this is your intended client and it still needs approval, run:</p>"
            "<pre>" + escape(approve_command) + "</pre>"
            "<p>This grants administrator access to this client. Approve only a registration you recognize. "
            "A new pairing code is not needed just to grant this permission.</p>"
            "<p>In your AI client, call <code>connection_status</code> with no arguments. "
            "Once it reports <code>admin_approved: true</code>, call <code>" + command_tool + "</code> with:</p>"
            "<pre>" + escape(sample) + "</pre>"
            "<p>Leave <code>cwd</code> omitted. Require <code>status: completed</code>, "
            "<code>exit_code: 0</code> and the expected hostname. A connected account or approved status alone "
            "does not verify command execution.</p></section>")

def _valid_browser_origin(request):
    """Allow exact Origin or strict same-origin Fetch Metadata + HTTPS Referer."""
    expected = os.environ.get("ASTER_MCP_PUBLIC_ORIGIN", "")
    supplied = request.headers.get("origin")
    if not expected:
        return False
    if supplied is not None:
        return supplied == expected
    if request.headers.get("sec-fetch-site") != "same-origin":
        return False
    referer = request.headers.get("referer")
    if not referer:
        # Pairing pages previously set Referrer-Policy: no-referrer.
        # Fetch Metadata still proves a same-origin browser submission.
        return True
    ref = urlsplit(referer)
    return (ref.scheme == "https"
            and ref.netloc == urlsplit(expected).netloc
            and ref.path.startswith("/pair/"))


def append_query(url: str, params: dict) -> str:
    parts=urlsplit(url)
    query=list(parse_qsl(parts.query,keep_blank_values=True))
    query.extend(params.items())
    return urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query),parts.fragment))

def owner_approval_app(db_path: str):
    approvals=ApprovalStore(db_path)
    codes=AuthorizationCodeStore(db_path)
    sessions=ApprovalSessions(db_path)

    def pairing_page(record, rid, nonce, *, new_browser=None, error=None):
        remaining = max(0, record["expires_at"] - int(time.time()))
        message = ("<p role='alert' data-error='invalid_or_expired_code'>"
                   "That code was not accepted. Enter the current code generated "
                   "on this server. You can retry using the form below.</p>") if error else ""
        body=("<!doctype html><html>" + onboarding_head("Aster MCP pairing") + "<body>"
              "<h1>Connect your MCP client</h1>"
              "<p>Client: "+escape(record["client_id"])+"</p>"
              "<p>Requested OAuth scope: "+escape(record["scope"])+"</p>"
              +message+
              "<p>Enter the one-time code generated locally on your server.</p>"
              "<p>This connection request expires in "+str(remaining)+" seconds. "
              "If it expires, restart the connection in your MCP client.</p>"
              "<form method='post' action='/pair/redeem'>"
              "<input type='hidden' name='request_id' value='"+escape(rid,quote=True)+"'>"
              "<input type='hidden' name='nonce' value='"+escape(nonce,quote=True)+"'>"
              "<label>Pairing code <input name='pairing_code' type='password' required autocomplete='off'></label>"
              "<button type='submit'>Approve OAuth connection</button></form>"
              +administrator_onboarding(record)+
              "</body></html>")
        response=HTMLResponse(body,status_code=403 if error else 200,headers={
            "Cache-Control":"no-store",
            "Content-Security-Policy":"default-src 'none'; form-action 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy":"same-origin"})
        if new_browser:
            response.set_cookie("aster_pair_browser",new_browser,httponly=True,
                                secure=True,samesite="lax",max_age=300,path="/pair")
        return response

    async def status(request):
        rid=request.query_params.get("request_id","")
        record=approvals.get(rid)
        if record is None:
            return JSONResponse({"error":"unknown_or_expired"},status_code=404)
        browser = request.cookies.get("aster_pair_browser", "")
        if not record["approved"]:
            new_browser = None
            nonce = sessions.issue_completion(rid, browser) if browser else None
            if nonce is None:
                new_browser = sessions.bind_browser(rid)
                if new_browser is None:
                    return JSONResponse({"error":"pairing_session_already_bound"},status_code=403)
                nonce = sessions.issue_completion(rid, new_browser)
            if nonce is None:
                return JSONResponse({"error":"browser_session_required"},status_code=403)
            return pairing_page(record,rid,nonce,new_browser=new_browser)
        nonce = sessions.issue_completion(rid,browser) if browser else None
        if nonce is None:
            return JSONResponse({"error":"browser_session_required"},status_code=403)
        body = ("<!doctype html><html>" + onboarding_head("Finish Aster OAuth pairing") + "<body>"
                "<h1>Owner approved the OAuth connection</h1>"
                "<form method='post' action='/pair/complete'>"
                "<input type='hidden' name='request_id' value='" + escape(rid, quote=True) + "'>"
                "<input type='hidden' name='nonce' value='" + escape(nonce, quote=True) + "'>"
                "<button type='submit'>Continue to client</button></form>"
                +administrator_onboarding(record)+"</body></html>")
        return HTMLResponse(body, headers={"Cache-Control":"no-store",
            "Content-Security-Policy":"default-src 'none'; form-action 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy":"same-origin"})

    async def redeem(request):
        if not _valid_browser_origin(request):
            return JSONResponse({"error":"invalid_origin"},status_code=403)
        if request.headers.get("content-type","").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return JSONResponse({"error":"invalid_content_type"},status_code=415)
        form=await request.form()
        rid=str(form.get("request_id",""))
        nonce=str(form.get("nonce",""))
        browser=request.cookies.get("aster_pair_browser","")
        code=str(form.get("pairing_code",""))
        record=approvals.get(rid)
        if not record or record["approved"] or record["scope"]!="system:admin":
            return JSONResponse({"error":"invalid_request"},status_code=403)
        if not browser or not sessions.consume_nonce(rid,browser,nonce):
            return JSONResponse({"error":"invalid_session"},status_code=403)
        if not PairingStore(db_path).consume(code):
            record=approvals.get(rid)
            retry_nonce=sessions.issue_completion(rid,browser) if record else None
            if retry_nonce is None:
                return JSONResponse({"error":"authorization_expired",
                                     "message":"Restart the connection in your MCP client."},
                                    status_code=403,headers={"Cache-Control":"no-store"})
            return pairing_page(record,rid,retry_nonce,error=True)
        if not approvals.approve(rid):
            return JSONResponse({"error":"approval_failed"},status_code=409)
        return RedirectResponse("/pair/authorize?"+urlencode({"request_id":rid}),
                                status_code=303,headers={"Cache-Control":"no-store","Referrer-Policy":"same-origin"})

    async def complete(request):
        if not _valid_browser_origin(request):
            return JSONResponse({"error":"invalid_origin"},status_code=403)
        # TLS is terminated by Caddy; the internal ASGI URL is HTTP.
        # Compare against the configured canonical HTTPS origin instead.
        content_type = request.headers.get("content-type","").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return JSONResponse({"error":"invalid_content_type"},status_code=415)
        form = await request.form()
        rid = str(form.get("request_id", ""))
        nonce = str(form.get("nonce", ""))
        browser = request.cookies.get("aster_pair_browser", "")
        if not browser or not sessions.consume(rid,browser,nonce):
            reason=sessions.completion_diagnostic(rid,browser,nonce)
            return JSONResponse({"error":"invalid_completion","reason":reason},status_code=403,
                                headers={"Cache-Control":"no-store"})
        approved=approvals.consume_approved(rid)
        if approved is None:
            return JSONResponse({"error":"already_used_or_expired"},status_code=409)
        code=codes.issue_approved(client_id=approved["client_id"],
                                  redirect_uri=approved["redirect_uri"],
                                  resource=approved["resource"],
                                  scope=approved["scope"],
                                  code_challenge=approved["code_challenge"])
        params={"code":code}
        if approved["state"] is not None:
            params["state"]=approved["state"]
        callback=append_query(approved["redirect_uri"],params)
        # Some Android browser flows do not follow the external 303 navigation.
        # Provide an explicit user-initiated HTTPS link instead.
        if urlsplit(callback).scheme != "https" or callback.split("?")[0] != approved["redirect_uri"].split("?")[0]:
            return JSONResponse({"error":"unsupported_callback_host"},status_code=403)
        body=("<!doctype html><html>" + onboarding_head("Return to MCP client") + "<body>"
              "<h1>OAuth pairing complete</h1>"
              "<p>Follow this link to finish connecting your MCP client.</p>"
              "<a href='"+escape(callback,quote=True)+"'>Return to MCP client</a>"
              +administrator_onboarding(approved)+
              "</body></html>")
        return HTMLResponse(body,status_code=200,headers={
            "Cache-Control":"no-store",
            "Referrer-Policy":"no-referrer",
            "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})

    return Starlette(routes=[Route("/pair/authorize",status,methods=["GET"]),
                             Route("/pair/complete",complete,methods=["POST"]),
                             Route("/pair/redeem",redeem,methods=["POST"])])
