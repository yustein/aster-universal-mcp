# Windows technical beta: install, connect and operate

This guide installs the manually started, source-based Windows beta. Use a disposable Windows machine first and keep an elevated local PowerShell session available. Commands run with the gateway account's privileges. Read [SECURITY.md](../SECURITY.md) before approving a remote client.

The reference environment is **Windows 11 Enterprise Evaluation 26H2 build 26300.9457 and Python 3.11.9 x64**. The release acceptance asset records the exact environment tested for its archive. Windows 10, other Windows builds and other Python versions are not qualified by that result.

## 1. Obtain and verify the release package

Download these matching assets from the project's [Releases page](https://github.com/yustein/aster-universal-mcp/releases):

- `aster-universal-mcp-0.1.0b1-windows.zip`
- `SHA256SUMS.txt`
- `windows-beta-0.1.0b1-acceptance.json`

Check that the acceptance record names the same ZIP and records passed required gates. In the folder containing the downloads, calculate the ZIP hash and compare the complete value with its entry in the trusted release checksum file:

~~~powershell
Get-FileHash -LiteralPath .\aster-universal-mcp-0.1.0b1-windows.zip -Algorithm SHA256
~~~

Extract to a new, empty source directory:

~~~powershell
Expand-Archive -LiteralPath .\aster-universal-mcp-0.1.0b1-windows.zip -DestinationPath C:\Aster\0.1.0b1
Set-Location C:\Aster\0.1.0b1
~~~

The extracted directory contains `scripts`, the dependency lists and `candidate-manifest.json` directly. Keep the ZIP, checksums and acceptance record outside this source directory. Use a dedicated source path and do not store work products in it.

The manifest hashes source bytes. The supplied archive preserves those bytes; `.gitattributes` also keeps source line endings at LF in Git checkouts. A development checkout still needs the correct reviewed commit and manifest. Do not regenerate a manifest to make an unexplained verification failure disappear.

## 2. Prepare the machine and dependencies

You need an installed Python x64 interpreter, an administrator account, correct system time, and a stable HTTPS hostname you control. The tested interpreter is Python 3.11.9 x64. The release's exact dependency lists are:

| Installation | Dependency file selected by setup |
|---|---|
| Runtime | `requirements-windows-lock.txt` |
| Runtime plus tests | `requirements-windows-test-lock.txt` with `-IncludeTests` |

Use a new source directory for a release rather than upgrading packages in an active deployment. Existing installations must be stopped before setup; the helper refuses an occupied port or an existing recorded live process.

In an **elevated PowerShell terminal**, change to the extracted source directory. Select the installed Python 3.11 interpreter explicitly:

~~~powershell
$asterPython = (& py.exe -3.11 -c "import sys; print(sys.executable)").Trim()
& $asterPython --version
~~~

If the launcher is unavailable, set `$asterPython` to the installed interpreter's absolute path, for example `C:\Program Files\Python311\python.exe`. Compare its reported version and architecture with the release acceptance environment.

Choose your real hostname now. Replace `https://mcp.yourdomain.example` throughout this guide with that **same bare HTTPS origin**, without `/mcp` or a trailing path.

For an online installation:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -PublicOrigin https://mcp.yourdomain.example -PythonExecutable $asterPython -IncludeTests
~~~

The `Bypass` option is scoped to this launched process; it does not change the computer's permanent execution policy. Run helpers only from the verified release.

For offline installation, prepare the wheels on an Internet-connected Windows x64 machine using the same Python version and the chosen lock file:

~~~powershell
& $asterPython -m pip download --only-binary=:all: --requirement .\requirements-windows-test-lock.txt --dest C:\AsterDownloads\wheels
~~~

Transfer the release assets and wheel directory to the offline machine. Then use:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -PublicOrigin https://mcp.yourdomain.example -PythonExecutable $asterPython -Wheelhouse D:\wheels -IncludeTests
~~~

Adjust the wheel-directory path. The helper adds `--no-index`, so a missing wheel fails installation rather than falling back to the network. The runtime lock can be used instead when tests are not requested. The optional wheel directory belongs outside frozen source.

Setup verifies the source, installs into `.venv` using the selected exact dependency list, runs `pip check`, records the installed inventory and protects the state directory. Its final JSON must say `configured`. Retain its candidate ID and dependency inventory for support.

## 3. Know where data belongs

| Item | Default location |
|---|---|
| Source and Python environment | Your extracted source directory and its `.venv` |
| Configuration | `%ProgramData%\AsterUniversalMCP\gateway-config.json` |
| OAuth database | `%ProgramData%\AsterUniversalMCP\windows-oauth.sqlite` |
| Administrative client grants | `%ProgramData%\AsterUniversalMCP\admin-clients.json` |
| Frozen manifest and runtime record | `candidate-manifest.json` and `gateway-runtime.json` in the protected state directory |
| Default working folder | `%ProgramData%\AsterUniversalMCP\workspace` |
| Independent local recovery helper | `%ProgramData%\AsterUniversalMCP\operator-windows.ps1` |
| Job records and outputs | Normally `%ProgramData%\AsterUniversalMCP\jobs` |
| Transfer staging | Normally the gateway account's home directory under `AppData\Local\AsterUniversalMCP\transfers` |

Setup restricts the state directory and default workspace to SYSTEM and Administrators. Commands, jobs and relative directory listings use the default workspace when their working directory is omitted. An explicit `cwd` or absolute file path remains supported. This default organizes work; it does not restrict an approved client's access to other locations permitted by the gateway account.

Job and transfer roots can also be affected by an operator-supplied `ASTER_WINDOWS_DATA` environment variable. Record any such override. Stopping or disabling the gateway removes none of these files.

Existing OAuth state and client grants are retained when setup runs again. Keep a private backup when changing deployments; do not copy state from an unrelated machine or mix a new helper with an old running gateway.

## 4. Start and verify the gateway

For manual foreground operation, keep this elevated terminal open:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1
~~~

Alternatively, `-Background` starts the same manual gateway, waits for readiness and records `gateway-stdout.log` and `gateway-stderr.log` in the protected state directory:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1 -Background
~~~

In a second elevated terminal, from the source directory:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\status-windows.ps1
~~~

Proceed only when the result says `verified_running`. This verifies source bytes, configuration and workspace paths, candidate identity, actual process image and creation time, and ownership of the loopback listener. A saved runtime JSON file alone is not proof that the correct process is running.

Manual launch does not install a Windows service or arrange startup after a reboot. After a reboot, start the tunnel and gateway manually and verify status before using the connector.

## 5. Configure your public HTTPS route

Claude's remote connector reaches your server from Anthropic's cloud. The route therefore needs a publicly reachable HTTPS hostname. An owner-managed proxy or tunnel must meet these requirements:

| Setting | Required value |
|---|---|
| Public origin | Exactly the HTTPS origin supplied to setup |
| MCP URL entered in Claude | That origin followed by `/mcp` |
| Local gateway destination | `http://127.0.0.1:18767` |
| Host forwarded to Aster | The configured public hostname |
| Forwarded scheme | `X-Forwarded-Proto: https` |
| Authentication | Aster's OAuth routes must be reachable by the remote client; do not add an interactive proxy login that the connector cannot complete |

The Aster helpers do not create networking or firewall rules. Keep the raw gateway port on loopback. If the tunnel runs on another machine, use an explicitly restricted private route to a local proxy; that topology needs its own validation.

### Example: a named Cloudflare Tunnel on the Windows machine

Install `cloudflared` using [Cloudflare's official instructions](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/). With your domain in that account, create a dedicated tunnel:

~~~powershell
cloudflared tunnel login
cloudflared tunnel create aster-mcp
cloudflared tunnel route dns aster-mcp mcp.yourdomain.example
~~~

Use the returned UUID and credential-file path in a dedicated configuration stored outside source:

~~~yaml
tunnel: YOUR-TUNNEL-UUID
credentials-file: 'C:\Users\YOUR-USER\.cloudflared\YOUR-TUNNEL-UUID.json'
ingress:
  - hostname: mcp.yourdomain.example
    service: http://127.0.0.1:18767
  - service: http_status:404
~~~

Run it using that file's actual path:

~~~powershell
cloudflared tunnel --config C:\AsterTunnel\config.yml run aster-mcp
~~~

Protect the tunnel credentials. The gateway and tunnel both need to remain running during this manual-mode beta.

### Check the route before pairing

~~~powershell
curl.exe -fsS -A "Aster-Setup-Check/1.0" https://mcp.yourdomain.example/.well-known/oauth-authorization-server
curl.exe -i -A "Aster-Setup-Check/1.0" -X POST https://mcp.yourdomain.example/mcp -H "Content-Type: application/json" -d "{}"
~~~

Discovery should return the configured origin's OAuth metadata. An unauthenticated MCP request should be denied with HTTP 401. These are route checks; successful pairing and an actual tool result are separate checks. Gateway responses carry `x-aster-request-id`; a proxy/edge error without that header may have occurred before the request reached Aster.

## 6. Pair and approve the exact Claude client

In Claude, open **Customize → Connectors → + Add → Add custom connector**. Enter your HTTPS MCP URL. Choose **Sign in now**, OAuth client **Register automatically**, and leave request headers empty. UI wording can vary by account; consult [Claude's official guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

When the Aster pairing page is open, issue a fresh code locally:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\issue-pairing-windows.ps1
~~~

Enter that code on **your own Aster HTTPS origin**, complete approval and return to Claude.

A code has 12 characters, displayed as `XXXX-XXXX-XXXX`. Spaces, hyphens and lowercase are accepted. A new code invalidates the previous one. The code can last 15 minutes, while the current browser authorization request has a separate five-minute deadline. Generate it only when a fresh pairing page is open.

Inspect the registered client locally:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action clients
~~~

Check the intended client's name, exact ID and callback. Then approve that ID:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action approve -ClientId 'EXACT_CLIENT_ID'
~~~

Use `-Action allowed` to inspect grants. Setup, startup and pairing do not approve clients automatically. A successful browser connection alone does not grant privileged execution.

## 7. Verify useful work and session renewal

Enable the connector in a Claude conversation. Ask it to call `execute_command` separately with:

~~~json
{"command":["C:\\Windows\\System32\\hostname.exe"],"timeout":10}
{"command":["C:\\Windows\\System32\\whoami.exe"],"timeout":10}
~~~

For each actual tool result, check `status: completed`, `exit_code: 0` and the expected machine/account. HTTP 200 or the word “Connected” alone does not establish command success.

Next, ask Claude to create, read and remove one uniquely named test file in the default workspace without specifying a working directory. Rerun `status-windows.ps1` locally; it should still verify the frozen candidate. Keep normal work files out of the source folder, including the release archive and evidence files.

For a background-job check, use a harmless command with an explicit short `max_runtime`, record the returned job ID, then call `background_job_status`, `cancel_background_job` and status again. Retain the actual result and verify termination when qualifying a release. File-transfer acceptance also checks downloaded size and SHA-256 against the uploaded content.

Access tokens last 15 minutes and the server implements rotating refresh tokens. Exercise another harmless command after that first token expires; record renewal and the successful tool result. A failure at that point needs diagnosis rather than an assumption that repeated manual pairing is required.

To run the included component tests in a test installation:

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q tests
~~~

See [release status](RELEASE_STATUS.md) for the distinction between controlled public protocol tests and observations from a real Claude account. Record only what was actually exercised.

## Disable access and stop

Use the **protected operator script installed by verified setup** for recovery. It reads the protected deployment state without importing repository Python and remains usable if source verification fails.

From an elevated PowerShell terminal, inspect the currently approved client IDs:

~~~powershell
$asterOperator = Join-Path $env:ProgramData 'AsterUniversalMCP\operator-windows.ps1'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $asterOperator -Action allowed
~~~

Remove one client's administrative permission:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $asterOperator -Action revoke -ClientId 'EXACT_CLIENT_ID'
~~~

Disable all administrative grants and stop the verified gateway:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $asterOperator -Action disable -StopGateway
~~~

For a planned stop that preserves grants and state:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $asterOperator -Action stop
~~~

The source wrappers `scripts\disable-windows.ps1` and `scripts\stop-windows.ps1` are convenient in an intact installation. Use the protected path directly when investigating source drift. New startup, pairing and approval still require verified source; the recovery commands do not bypass those requirements.

Cancel and verify outstanding jobs before a planned stop. Revocation or gateway shutdown prevents new privileged requests but does not undo completed work or implicitly cancel all existing jobs and child processes. If a stop refuses an uncertain process identity, disable the dedicated public route and inspect the local process before intervening.

## Restart and remove

For a manual restart, cancel/check jobs, run the protected `stop` action, then run `start-windows.ps1` and `status-windows.ps1` from the same verified release. The OAuth database and grants remain in place. Confirm a harmless authenticated operation afterwards; issue another owner code only if a new authorization flow is actually required.

To remove the installation:

1. Disable administrative grants and stop the verified gateway using the protected helper. Handle any outstanding jobs or child processes locally.
2. Stop and remove only this installation's dedicated tunnel/proxy route, then remove its custom connector in Claude.
3. Save any work products or private diagnostic material you need. Do not put OAuth databases or tunnel credentials in public reports.
4. Remove the chosen source directory and its `.venv` when no process uses them.
5. Decide explicitly whether to retain or delete `%ProgramData%\AsterUniversalMCP`. It contains private OAuth state, grants, logs, job records and the workspace. Deleting it removes those records and requires fresh setup/pairing later.
6. Check the gateway account's transfer-staging directory and any `ASTER_WINDOWS_DATA` override. Partial transfer buffers and files written to other explicit destinations are separate from the source directory and are not deleted automatically.

No helper performs a broad uninstall or deletes user work. Avoid deleting a shared tunnel, unrelated Python installation or another deployment's files.

## Troubleshooting

| Symptom | Next check |
|---|---|
| Source verification is blocked | Use the protected recovery helper if access must be disabled. Inspect changed/missing/extra files and restore trusted release bytes. Keep unexpected outputs outside source; do not refreeze unexplained changes. |
| Gateway port is occupied | Identify its owner through status/runtime evidence. Stop only the intended verified process. |
| Pairing page expired or code was rejected | Capture the error, begin a fresh authorization request, then issue one fresh code. The code and browser request have separate deadlines. |
| Claude says connected but tools are denied | Check the exact client's local allowlist entry and the actual structured error. Pairing does not automatically approve administration. |
| Discovery fails or the edge returns 403 | Check DNS, the dedicated tunnel and forwarded Host/HTTPS headers. An error without an Aster request ID may be from the edge. Do not weaken the gateway's origin checks. |
| Commands fail after an idle period | Check the token/refresh exchange and actual request results. Keep bearer values out of diagnostics. |
| A relative write changes the application source | Confirm the configured `workspace_root` and actual process identity belong to this release. Stop and diagnose a mixed or old deployment. |
| A helper reports an uncertain running process | Preserve the runtime evidence, disable the public route if needed and identify the process locally before stopping it. |

After one failed account attempt, diagnose the recorded transaction before issuing more codes or making more changes. Freeze and validate a new candidate whenever application source changes.
