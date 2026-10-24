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
        body=("<!doctype html><html><head><meta charset='utf-8'>"
              "<title>Aster MCP pairing</title></head><body>"
              "<h1>Connect your MCP client</h1>"
              "<p>Client: "+escape(record["client_id"])+"</p>"
              "<p>Permissions: "+escape(record["scope"])+"</p>"
              +message+
              "<p>Enter the one-time code generated locally on your server.</p>"
              "<p>This connection request expires in "+str(remaining)+" seconds. "
              "If it expires, restart the connection in your MCP client.</p>"
              "<form method='post' action='/pair/redeem'>"
              "<input type='hidden' name='request_id' value='"+escape(rid,quote=True)+"'>"
              "<input type='hidden' name='nonce' value='"+escape(nonce,quote=True)+"'>"
              "<label>Pairing code <input name='pairing_code' type='password' required autocomplete='off'></label>"
              "<button type='submit'>Approve connection</button></form>"
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
        body = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
                "<h1>Owner approved this connection</h1>"
                "<form method='post' action='/pair/complete'>"
                "<input type='hidden' name='request_id' value='" + escape(rid, quote=True) + "'>"
                "<input type='hidden' name='nonce' value='" + escape(nonce, quote=True) + "'>"
                "<button type='submit'>Continue to client</button></form></body></html>")
        return HTMLResponse(body, headers={"Cache-Control":"no-store",
            "Content-Security-Policy":"default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
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
        body=("<!doctype html><html><head><meta charset='utf-8'>"
              "<meta name='referrer' content='no-referrer'>"
              "<title>Return to MCP client</title></head><body>"
              "<h1>Authorization complete</h1>"
              "<p>Follow this link to finish connecting your MCP client.</p>"
              "<a href='"+escape(callback,quote=True)+"'>Return to MCP client</a>"
              "</body></html>")
        return HTMLResponse(body,status_code=200,headers={
            "Cache-Control":"no-store",
            "Referrer-Policy":"no-referrer",
            "Content-Security-Policy":"default-src 'none'; base-uri 'none'; frame-ancestors 'none'"})

    return Starlette(routes=[Route("/pair/authorize",status,methods=["GET"]),
                             Route("/pair/complete",complete,methods=["POST"]),
                             Route("/pair/redeem",redeem,methods=["POST"])])
