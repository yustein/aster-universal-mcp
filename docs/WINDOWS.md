# Windows: manual OAuth MCP beta for Claude

This source-based beta can execute commands with the privileges of its Windows gateway process. Use a disposable Windows machine for acceptance testing. The helpers below deliberately configure **manual elevated operation**, with private state accessible to SYSTEM and Administrators. They do not install a Windows service, create a tunnel, open firewall ports, or approve an OAuth client.

## Prerequisites

- Windows 10/11 x64 and an installed Python 3.11+ x64 interpreter.
- A complete, trusted copy of this repository. Git is useful but is not required for a verified source archive.
- A secured test account, correct UTC time, and a reliable way to retrieve guest command results and logs.
- For real Claude acceptance: a Claude Free account with its one custom remote connector available, and a public HTTPS origin you control.

Claude's remote connector reaches the gateway from Anthropic's cloud. A trusted HTTPS proxy or tunnel must forward to `http://127.0.0.1:18767`, preserve the configured public Host, and supply `X-Forwarded-Proto: https`. The gateway's loopback listener is not a public endpoint by itself. [Official Claude connector guidance](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

## One deployment layout

| Item | Location |
|---|---|
| Source | The directory containing this README and `scripts` |
| Python environment | `<source>\.venv\Scripts\python.exe` |
| Configuration | `%ProgramData%\AsterUniversalMCP\gateway-config.json` |
| OAuth database | `%ProgramData%\AsterUniversalMCP\windows-oauth.sqlite` |
| Administrator client allowlist | `%ProgramData%\AsterUniversalMCP\admin-clients.json` |
| Frozen source manifest | `%ProgramData%\AsterUniversalMCP\candidate-manifest.json` |
| Running-process record | `%ProgramData%\AsterUniversalMCP\gateway-runtime.json` |

Setup, startup, status, pairing, and client management all use this configuration. Do not copy an old user-profile database into this layout or mix an old running gateway with these helpers. Preserve old state separately when investigating a prior attempt. Existing OAuth state and client permissions are retained when setup runs again; they are not reset automatically.

## Freeze and install the source candidate

Obtain the chosen commit or reviewed source archive. In an **elevated PowerShell terminal**, change to that source directory. If the archive includes `candidate-manifest.json`, retain and verify it; do not regenerate it to make an unexpected verification failure disappear.

For a deliberately reviewed source checkout that does not yet have a manifest, freeze its current files once:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\create-windows-candidate.ps1
```

This records file hashes and reports a candidate ID. It establishes the identity of those files, not the authenticity of an unknown download. Review changes before generating a replacement manifest. The helpers reject modified, missing, or unexpected source files; `.git`, `.venv`, normal Python/test caches, and build/artifact output directories are excluded from the inventory.

Configure and install the candidate:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -PublicOrigin https://mcp.yourdomain.example -IncludeTests
```

The `Bypass` option applies only to the explicitly launched PowerShell process; it does not change the machine's execution policy. Run these commands only for source you trust.

If the Python launcher is unavailable, add `-PythonExecutable 'C:\Path\To\Python\python.exe'`. For a prepared offline dependency directory, add `-Wheelhouse 'D:\wheels'`; this adds `--no-index` and installs only from that directory. The offline set must satisfy both Windows and, when requested, test requirements.

Setup creates the repository's `.venv`, checks dependency consistency, records installed package versions, protects the state directory, and writes the shared configuration after source verification. It refuses an occupied gateway port or a still-running process recorded by the prior runtime manifest. It does not silently reuse a different source extraction and claim an upgrade succeeded.

The final JSON result must say `configured`. Record its `candidate_id`, source path, interpreter and dependency inventory with the test evidence.

## Start and verify the actual gateway

Keep an elevated terminal open for manual foreground operation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1
```

For an automated disposable-VM test, `-Background` starts the same manual gateway, waits for verified readiness, returns JSON and writes separate `gateway-stdout.log` and `gateway-stderr.log` files in the state directory. This is not service installation or autostart.

In a second elevated terminal, from the same source directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\status-windows.ps1
```

Proceed only when the result is `verified_running`. The status check verifies the frozen source, candidate identity, configured paths, live process image and process lifetime, and the owning PID of the loopback listener. A runtime JSON file alone is not proof of a live gateway. A failed check returns `blocked` and a nonzero exit code.

The gateway accepts only loopback traffic and the configured public origin. Set up the separately controlled public HTTPS route after local HTTP acceptance. A metadata response and an anonymous MCP denial are useful checks, but do not establish successful OAuth or tool execution.

## Complete a fresh Claude authorization request

1. In Claude, open **Customize → Connectors → Add → Custom → Web**. Enter `https://mcp.yourdomain.example/mcp` and choose automatic OAuth client registration.
2. When the Aster authorization page appears, issue one code from the verified Windows deployment:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\issue-pairing-windows.ps1
   ```

3. Enter the displayed code on your own Aster HTTPS origin and follow the return link to finish connecting the client.

The code has **12 characters**, displayed as `XXXX-XXXX-XXXX`, using an unambiguous alphabet. Spaces, hyphens and lowercase are accepted. Issuing another code invalidates the previous one. Successful consumption is one-time, and incorrect attempts are limited.

**There are separate deadlines:** the owner code can remain valid for 15 minutes, while the current OAuth authorization request and browser session have a five-minute lifetime. A new code does not extend an old browser request. If the request expired or the session changed, start authorization again before generating another code.

The pairing helper refuses to issue a code when the running process, listener, source candidate or database paths cannot be verified. Resolve the reported problem before retrying. Keep the `.cmd` convenience launcher in the repository's `scripts` directory; it delegates to the same PowerShell helper and requires elevation.

## Explicitly approve the intended client

OAuth pairing and a `system:admin` token do not by themselves grant command execution. Inspect the registered client and redirect URI locally:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action clients
```

After verifying the intended client's exact ID and callback, explicitly authorize it:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action approve -ClientId 'EXACT_CLIENT_ID'
```

Use `-Action allowed` to inspect the allowlist, or `-Action revoke -ClientId 'EXACT_CLIENT_ID'` to remove permission. Revocation can run without a live gateway when the frozen source/configuration and database are intact. No setup, startup or pairing helper approves clients automatically.

## Acceptance evidence and stop rule

Run component tests in the installed environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
```

Then establish the actual HTTP registration, browser-form/cookie/nonce handling, code approval and PKCE token-exchange flow against the running deployment. Repeat the applicable flow through public HTTPS and correlate it with the same Windows process. Direct calls to internal provider methods do not satisfy this gate.

The real Claude test must show tool discovery, denial before explicit client approval, successful identity/hostname checks afterwards, a small disposable file transfer with a matching hash, and a bounded background job with verified cancellation. Restart through the documented launch mode and repeat a harmless authenticated operation. Keep source/dependency identities, UTC timestamps, status JSON and redacted transaction logs with the result. Do not record pairing codes, tokens or authentication cookies in the acceptance report.

After one failed real Claude attempt, capture and diagnose that transaction before issuing more codes. Change one identified cause, verify the deployed candidate, and start a fresh authorization request. Changing source invalidates the frozen candidate; review and install the new candidate before continuing.

Manual console operation is a supported beta mode. A Windows service, unattended startup, native symlink security cases and public distribution require their own acceptance evidence. See [release status](RELEASE_STATUS.md) and [security boundaries](../SECURITY.md).
