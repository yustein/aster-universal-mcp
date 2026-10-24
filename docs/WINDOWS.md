# Windows v0.1.0b3 beta: install, connect and operate

This guide installs the manually started, source-based Windows beta. Use a disposable Windows machine first and keep an elevated local PowerShell session available. Commands run with the gateway account's privileges. Read [SECURITY.md](../SECURITY.md) before approving a remote client.

The b3 reference system reported **Windows 11 Enterprise Evaluation build 26300 and Python 3.11.9 x64**, with manual SYSTEM operation. Its exact artifact passed 126 native tests plus 7 subtests with 5 skips, a fresh runtime-only installation and 38 controlled public HTTPS stages. The earlier local HTTP attempt passed 11 stages before an external stopped-listener observer failed; it remains partial/failed. Independent reconciliation and the public run closed its remaining restart/revocation/cleanup cases. Test layers overlap and are not added. Windows 10, other Windows builds and other Python versions are not qualified by these results.

**[v0.1.0b3](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b3) is qualified with the owner's explicit waiver of repeat Claude-account testing.** No fresh b3 actual-account success is claimed. Windows and Linux have separate artifacts under that shared tag. B3 adds `connection_status`, actionable permission errors and bounded-listing metadata. Its source/tag remains tested commit `6d792d0920875bf3cb958b83744cca7a5fc1ec1e`; packaged candidate documentation was frozen before qualification. The [matching release/operator guide](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/ASTER_0.1.0b3_RELEASE_OPERATOR_ONBOARDING_GUIDE.md) and acceptance record supply the final status. Historical Windows `v0.1.0b1` remains unchanged; do not copy new source files into a frozen b1 installation.

Every new installation requires **install/start → OAuth pair → inspect the exact client ID and callback in a trusted local administrator terminal → approve it → check `connection_status` → run native hostname/identity calls → mark setup complete only after success**. The release owner's waiver does not remove these setup checks. [The shared onboarding guide](ONBOARDING.md) explains the states and version differences. No step grants administrator access automatically.

## 1. Obtain and verify the release package

Obtain the matching assets from the [b3 release](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b3):

- [aster-universal-mcp-0.1.0b3-windows.zip](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/aster-universal-mcp-0.1.0b3-windows.zip)
- [SHA256SUMS.txt](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/SHA256SUMS.txt)
- [windows-beta-0.1.0b3-acceptance.json](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/windows-beta-0.1.0b3-acceptance.json)

Check that the acceptance record names the same ZIP, records satisfied backend/final-reconciliation requirements, and identifies the account-test waiver explicitly. The separate manifest asset is `windows-candidate-manifest.json`; the ZIP contains its embedded copy as `candidate-manifest.json`. In the folder containing the downloads, calculate the ZIP hash and compare the complete value with its entry in the trusted release checksum file:

~~~powershell
Get-FileHash -LiteralPath .\aster-universal-mcp-0.1.0b3-windows.zip -Algorithm SHA256
~~~

Extract to a new, empty source directory:

~~~powershell
Expand-Archive -LiteralPath .\aster-universal-mcp-0.1.0b3-windows.zip -DestinationPath C:\Aster\0.1.0b3
Set-Location C:\Aster\0.1.0b3
~~~

The extracted directory contains `scripts`, the dependency lists and `candidate-manifest.json` directly. Keep the ZIP, checksums and acceptance record outside this source directory. Use a dedicated source path and do not store work products in it.

The manifest hashes source bytes. The supplied archive preserves those bytes; `.gitattributes` also keeps source line endings at LF in Git checkouts. A development checkout still needs the correct reviewed commit and manifest. Do not regenerate a manifest to make an unexplained verification failure disappear.

## 2. Prepare the machine and dependencies

You need an installed Python x64 interpreter, an administrator account, correct system time, and a stable HTTPS hostname you control. The tested interpreter is Python 3.11.9 x64. The release's exact dependency lists are:

| Installation | Dependency file selected by setup |
|---|---|
| Runtime | `requirements-windows-lock.txt` |
| Runtime plus tests | `requirements-windows-test-lock.txt` with `-IncludeTests` |

Use a new source directory rather than upgrading packages in an active deployment. The examples below assume fresh protected state and an unused listener. Do not stop or reconfigure a working b1 connection merely to try b3; use a disposable machine or an explicitly verified isolated installation. The helper refuses an occupied port or an existing recorded live process, but that guard is not a migration or isolation procedure. Upgrade a live connection only after its state-preserving procedure is verified.

In an **elevated PowerShell terminal**, change to the extracted source directory. Select the installed Python 3.11 interpreter explicitly:

~~~powershell
$asterPython = (& py.exe -3.11 -c "import sys; print(sys.executable)").Trim()
& $asterPython --version
~~~

If the launcher is unavailable, set `$asterPython` to the installed interpreter's absolute path, for example `C:\Program Files\Python311\python.exe`. Compare its reported version and architecture with the release acceptance environment.

Choose your real hostname now. Replace `https://mcp.yourdomain.example` throughout this guide with that **same bare HTTPS origin**, without `/mcp` or a trailing path.

For an online runtime-only installation:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -PublicOrigin https://mcp.yourdomain.example -PythonExecutable $asterPython
~~~

The `Bypass` option is scoped to this launched process; it does not change the computer's permanent execution policy. Run helpers only from the verified release.

For offline installation, prepare the wheels on an Internet-connected Windows x64 machine using the same Python version and the chosen lock file:

~~~powershell
& $asterPython -m pip download --only-binary=:all: --requirement .\requirements-windows-lock.txt --dest C:\AsterDownloads\wheels
~~~

Transfer the release assets and wheel directory to the offline machine. Then use:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -PublicOrigin https://mcp.yourdomain.example -PythonExecutable $asterPython -Wheelhouse D:\wheels
~~~

Adjust the wheel-directory path. The helper adds `--no-index`, so a missing wheel fails installation rather than falling back to the network. Keep test dependencies in a separate test installation: `-IncludeTests` selects `requirements-windows-test-lock.txt` there. Complete fresh runtime-only installation checks before test packages could conceal missing runtime imports. The optional wheel directory belongs outside frozen source.

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

Existing OAuth state and client grants are retained when setup runs again. That retention is not an isolated-candidate installation: do not invoke new-release setup against the working b1 state. Keep a private backup for an explicitly qualified upgrade; do not copy state from an unrelated machine or mix a new helper with an old running gateway.

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

Proceed only when the result says `verified_running`. This verifies source bytes, configuration and workspace paths, candidate identity, actual process image and creation time, and ownership of the loopback listener. A saved runtime JSON file alone is not proof that the correct process is running. `verified_running` means the runtime is ready; it does not mean that a connector is paired or approved, or that setup is complete.

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

In the trusted elevated local PowerShell terminal, inspect the registered client:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action clients
~~~

Check the intended client's name, exact ID and registered callback against the connector and owner interaction you initiated. Then approve only that ID:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action approve -ClientId 'EXACT_CLIENT_ID'
~~~

Confirm the grant locally:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action allowed
~~~

The intended ID must appear in the result. Setup, startup and pairing do not approve clients automatically. A successful browser connection alone does not grant privileged execution. These approval commands belong in the trusted local terminal; an unapproved remote connector cannot approve itself.

## 7. Check connection status and prove native execution

**For b3:** enable the connector in the intended conversation and call `connection_status` with `{}`. Match its `client_id` to the locally approved ID; require `status: "approved"`, `admin_approved: true` and permission flags for commands, jobs, file transfers and directory listing. Inspect `default_workspace`, `listing_base` and `directory_listing` to confirm the intended paths/scope. This authenticated tool reports only its caller's state and never grants permission. Status is a diagnosis step, not proof that a native command ran.

If status reports `approval_required` or a privileged call returns `ADMIN_APPROVAL_REQUIRED`, inspect and approve the exact registration locally before retrying the harmless call. If `AUTHORIZATION_STATE_UNAVAILABLE` is returned, inspect the protected allowlist/configuration rather than treating it as ordinary missing approval. Do not repeat OAuth pairing to compensate for a missing local grant.

Status requires valid OAuth authentication with `system:admin` scope. `OAUTH_SCOPE_REQUIRED` is a different failure: a local allowlist grant cannot add a missing OAuth scope. Review that credential flow and actual error separately.

Published b1 has no `connection_status`; use its local `clients`/`allowed` check and the actual calls below. A missing tool on b1 is expected. B3 must pass the new status check as well as native commands before its setup is called complete.

Enable the connector in a Claude conversation. Ask it to call `execute_command` separately with:

~~~json
{"command":["C:\\Windows\\System32\\hostname.exe"],"timeout":10}
{"command":["C:\\Windows\\System32\\whoami.exe"],"timeout":10}
~~~

For each actual tool result, check `status: completed`, `exit_code: 0`, stdout/stderr and the expected machine/account. Omit `cwd` for this check so the configured workspace is used. **Setup is complete only after these actual calls succeed.** HTTP 200, a successful status response or the word “Connected” alone does not establish command success.

Next, ask Claude to create, read and remove one uniquely named test file in the default workspace without specifying a working directory. Rerun `status-windows.ps1` locally; it should still verify the frozen candidate. Keep normal work files out of the source folder, including the release archive and evidence files.

For a background-job check, use a harmless command with an explicit short `max_runtime`, record the returned job ID, then call `background_job_status`, `cancel_background_job` and status again. Retain the actual result and verify termination when qualifying a release. File-transfer acceptance also checks downloaded size and SHA-256 against the uploaded content.

Access tokens last 15 minutes and the server implements rotating refresh tokens. Exercise another harmless command after that first token expires; record renewal and the successful tool result. A failure at that point needs diagnosis rather than an assumption that repeated manual pairing is required.

To run the included component tests in a separate test installation prepared with `-IncludeTests`:

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q tests
~~~

See [release status](RELEASE_STATUS.md) for the distinction between controlled public protocol tests and observations from a real Claude account. Record only what was actually exercised.

## Directory paths and bounded listing

Windows `list_directory` requires the client's administrative grant on every call, including after token refresh. Approval enables paths accessible to the gateway account; it does not impersonate a different Windows account. `.` and relative paths start at the configured default workspace. Use an explicit absolute path to inspect another directory:

~~~json
{"path":"C:\\"}
{"path":"C:\\Windows"}
{"path":"."}
~~~

Each command has its own working directory. `cd` inside a shell command affects only that process; it does not change the next `execute_command`, job or listing call. Supply `cwd` on every command needing another directory. There is no persistent interactive-shell directory state between MCP calls.

Published b1 returns at most 100 entry names, without a completeness flag. B3 keeps the path-only tool signature and fixed 100-entry response limit, reads at most 101 entries, and adds `limit`, `truncated`, `total` and `total_is_exact`. It sorts only the bounded sample. When truncated, `total` is null and `total_is_exact` is false; 100 returned names must not be described as the directory's full size. Access failures remain errors rather than fabricated empty directories.

Revoking a client denies subsequent listings and other privileged tools. In b3, authenticated `connection_status` remains available to show its own revoked/unapproved state. Neither grant removal nor that status result revokes the OAuth credential family; use protected disable/stop for full remote shutdown.

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
| Claude says connected but tools are denied | B3: call `connection_status`, compare its ID with local `clients`/`allowed`, then follow `ADMIN_APPROVAL_REQUIRED` guidance only after verifying the intended registration. Pairing does not approve administration. Published b1 uses the local check directly. |
| `AUTHORIZATION_STATE_UNAVAILABLE` | Inspect protected allowlist ownership, readability and contents locally. Preserve unexpected state; do not turn this into a wildcard or automatic approval. |
| `connection_status` is absent | Inspect the actual installed artifact/tool list. Published b1 does not contain this b3 tool; a documentation update does not install it. |
| A listing stops after 100 names | Inspect truncation metadata on b3; b1 lacks it. Do not infer the full directory total from a capped response. |
| Discovery fails or the edge returns 403 | Check DNS, the dedicated tunnel and forwarded Host/HTTPS headers. An error without an Aster request ID may be from the edge. Do not weaken the gateway's origin checks. |
| Commands fail after an idle period | Check the token/refresh exchange and actual request results. Keep bearer values out of diagnostics. |
| A relative write changes the application source | Confirm the configured `workspace_root` and actual process identity belong to this release. Stop and diagnose a mixed or old deployment. |
| A helper reports an uncertain running process | Preserve the runtime evidence, disable the public route if needed and identify the process locally before stopping it. |

After one failed account attempt, diagnose the recorded transaction before issuing more codes or making more changes. Freeze and validate a new candidate whenever application source changes.
