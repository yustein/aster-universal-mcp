# Windows: full privileged Aster MCP over OAuth for Claude Free

**High-risk beta for technically experienced users.** After explicit local OAuth owner pairing and operator approval, Aster can run commands, manage background jobs, and transfer files via Claude. Test in an isolated Windows VM first.

## Prerequisites

Windows 10/11 x64, Python 3.11+, Git, a Claude Free account (one custom remote connector), and your own public HTTPS hostname such as https://mcp.yourdomain.example. A trusted HTTPS proxy or named tunnel must forward the public host to http://127.0.0.1:18767 while preserving the public Host and X-Forwarded-Proto: https headers.

## Install source

Open PowerShell:

    git clone https://github.com/yustein/aster-universal-mcp.git
    cd aster-universal-mcp
    py -3 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt

There is no mandatory one-click installer. Do not run the Python server as Administrator except when deliberately testing elevated host control.

## Configure private state

From an **elevated** PowerShell session on an isolated machine you control:

    $data = Join-Path $env:ProgramData "AsterUniversalMCP"
    New-Item -ItemType Directory -Force $data | Out-Null
    "[]" | Set-Content -Encoding utf8 (Join-Path $data "admin-clients.json")
    $env:ASTER_WINDOWS_PUBLIC_ORIGIN = "https://mcp.yourdomain.example"
    $env:ASTER_WINDOWS_OAUTH_DB = Join-Path $data "windows-oauth.sqlite"
    $env:ASTER_WINDOWS_ADMIN_CLIENTS = Join-Path $data "admin-clients.json"
    $env:ASTER_WINDOWS_PORT = "18767"

Set restrictive Windows ACLs on the data directory: SYSTEM and Administrators only. Never expose this directory, pairing codes or database over HTTP or via GitHub.

## Start OAuth gateway

From the repository root and the **same elevated PowerShell session**:

    .\.venv\Scripts\python.exe platforms\windows\oauth_gateway.py

The gateway listens only on 127.0.0.1:18767. A secure external HTTPS proxy/tunnel is required for Claude Free. The remote MCP URL is https://mcp.yourdomain.example/mcp.

The gateway inherits the privileges of its launching Windows account. If it is elevated, a correctly authorized Claude client can initiate powerful operations. Do not enable automatic administrative services until separately accepted.

## Important: use the *same* database as your running gateway

The code generator and HTTP gateway **must** use the exact same OAuth SQLite file. Running `pairing_admin.py issue` without `--db` on Windows is rejected rather than silently creating a different database.

This documentation's standard example launches with `C:\ProgramData\AsterUniversalMCP\windows-oauth.sqlite`. **Our disposable Lenomila test VM is different:** its gateway uses `C:\Users\astertest\AsterClaudeBeta\state\windows-oauth.sqlite`. When diagnosing that VM, pass that precise path to `pairing_admin.py --db ... issue --ttl 900`.

Issuing a second code invalidates the first immediately. The pairing code expires after its TTL; an outstanding OAuth authorization request has its own, shorter five-minute expiry. Restart OAuth authorization before pairing if you have been delayed. Preserve case and every character of the 43-character URL-safe code: `O` and `0`, or `l` and `1`, are different.

## Add to Claude Free

1. Open Claude > Customize > Connectors > Add > Custom > Web.
2. Enter the HTTPS MCP URL. Choose Sign in now and **Register automatically** for OAuth client registration.
3. When Aster's pairing page appears, in a second trusted elevated PowerShell terminal issue an owner code using the **same OAuth DB path**:

    .\.venv\Scripts\python.exe pairing_admin.py --db "C:\ProgramData\AsterUniversalMCP\windows-oauth.sqlite" issue

4. Enter the short-lived code on your own Aster HTTPS origin and complete the OAuth callback.

## Explicitly enable privileged tools for Claude

After pairing, inspect registered clients locally:

    .\.venv\Scripts\python.exe admin_client_allowlist.py --db "C:\ProgramData\AsterUniversalMCP\windows-oauth.sqlite" --allowlist "C:\ProgramData\AsterUniversalMCP\admin-clients.json" clients

Verify the intended client's ID and redirect URI. Then authorize that **exact** client:

    .\.venv\Scripts\python.exe admin_client_allowlist.py --db "C:\ProgramData\AsterUniversalMCP\windows-oauth.sqlite" --allowlist "C:\ProgramData\AsterUniversalMCP\admin-clients.json" approve CLIENT_ID

To revoke, replace approve with revoke. OAuth pairing and a system:admin token alone are not sufficient for execution: the independent owner-maintained allowlist is checked for each privileged tool call.

## Test

    .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    .\.venv\Scripts\python.exe -m pytest -q tests

In Claude, test whoami, a disposable background job, cancellation, and a small transfer inside a test directory. Review every tool invocation. **Do not test on real customer or production files.**

Security: [SECURITY.md](../SECURITY.md). These source and SDK tests are not a substitute for a real Claude Free OAuth connection, verified HTTPS public ingress, service deployment review, or a third-party privileged-execution audit.
