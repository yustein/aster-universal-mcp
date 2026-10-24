# Complete onboarding: pair, approve and verify

**Shared `v0.1.0b3` candidate — qualification/publication pending.** This procedure describes the new onboarding flow for separate Windows and Linux artifacts intended under the same tag. Its authenticated `connection_status` tool, actionable permission errors and permission-aware Linux listing require newly frozen and qualified platform artifacts. They are not present in the unchanged published Windows `v0.1.0b1` and Linux `v0.1.0b2` artifacts. Those releases retain their own acceptance evidence. Do not patch files inside an installed frozen release or regenerate its manifest to obtain these changes.

Installation and a running gateway are prerequisites. OAuth pairing authenticates a connector. A separate local grant authorizes administration. **Setup is complete only when the intended connector has been explicitly approved and harmless native commands have actually succeeded.** There is no automatic administrator/root grant.

## 1. Install, start and verify the runtime

Follow the matching [Windows](WINDOWS.md) or [Linux](LINUX.md) installation guide using the exact b3 artifact and platform acceptance record. The intended version is `v0.1.0b3`; qualification and publication are pending. Candidate testing must keep independent local administrator control and use isolated state/listeners, preserving existing live b1/b2 connections until a qualified upgrade. The new platform guides use b3 package/source-folder examples rather than treating an old installed gateway as the new candidate.

Windows local status must report `verified_running`. Linux must report both canonical processes/endpoints ready with source and venv verified. Verify the dedicated public HTTPS route. These checks establish runtime readiness; they do not establish an approved client or a successful command.

## 2. Pair the intended connector through OAuth

Add the installation's HTTPS `/mcp` URL in the connector account, choose sign-in and automatic client registration where offered, and open the Aster authorization page. Issue one short-lived code locally when the page is ready.

Windows, from an elevated PowerShell terminal in the verified source directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\issue-pairing-windows.ps1
```

Linux, using the independently installed protected operator:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp pair
```

Use the actual protected state path if this installation deliberately uses another one. Enter the code only at your own Aster HTTPS origin and complete the redirect. Keep codes, cookies and tokens private. Pairing-code and browser-request lifetimes differ. A completed OAuth redirect does not place the client in the administrator allowlist.

## 3. Inspect the exact registration locally

Keep this step in the trusted local administrator terminal. Inspect the intended client's ID, name and registered callback/redirect URI; match them to the connector and authorization interaction you initiated. Do not approve every listed registration or choose a similarly named client by guesswork.

Windows:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action clients
```

Linux:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp clients
```

## 4. Explicitly approve that client

Replace `EXACT_CLIENT_ID` with the locally verified ID. These are local administrator commands, not commands to ask an unapproved remote connector to execute.

Windows:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action approve -ClientId 'EXACT_CLIENT_ID'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage-client-windows.ps1 -Action allowed
```

Linux:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp approve EXACT_CLIENT_ID
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp clients
```

On Linux, confirm `admin_allowed: true` for that exact registration. Windows's `allowed` output must contain the intended ID. Both approval helpers require the verified deployment; investigate unexpected source/configuration state rather than bypassing verification.

## 5. Check this connector's authenticated status

In the actual connector conversation, call **`connection_status` with `{}`**. Check its reported `client_id` against the one approved locally, `status: "approved"`, `admin_approved: true`, the relevant `permissions` flags, `default_workspace` and effective `directory_listing` scope. It also reports `authenticated`, `platform`, `listing_base` and a `next_step`. The tool reports its own authenticated connection; it does not grant permission or list other registered clients.

`permissions` distinguishes commands, jobs, file transfers, directory listing and listing outside the configured root. `directory_listing` reports its scope, relative-path base, configured root and whether explicit absolute paths outside that root are allowed. On Linux, `listing_base` can differ from the command/job `default_workspace` if the operator deliberately configured separate roots; the default installation uses the workspace for both.

An unapproved client receives `status: "approval_required"` and can use this tool to understand what is missing. A privileged call denied for missing approval returns the actionable `ADMIN_APPROVAL_REQUIRED` tool error. Follow the platform's local inspection/approval procedure after verifying the intended client. `AUTHORIZATION_STATE_UNAVAILABLE` means the allowlist could not be trusted/read; diagnose that protected state locally instead of assuming an absent grant. Do not repeat OAuth pairing solely to repair a missing local grant. After approval, call status again from the same connector.

Status itself requires valid OAuth authentication with `system:admin` scope. `AUTHENTICATION_REQUIRED` or `OAUTH_SCOPE_REQUIRED` indicates a different credential problem for the owner to review. Local approval cannot supply a missing OAuth scope. The expected unapproved status assumes valid credentials and readable protected authorization state.

If the tool is absent, inspect the installed version and its actual tools list. Published b1/b2 do not have it. Use their documented local allowlist inspection plus actual command verification, or plan a separately verified installation of the new release when available. Do not label the new candidate's setup check passed using an old gateway.

## 6. Run harmless native commands through MCP

Call each command separately, omitting `cwd` so the configured workspace is used. Use the server's actual platform-specific tool name.

Windows: call `execute_command` with each of these arguments:

```json
{"command":["C:\\Windows\\System32\\hostname.exe"],"timeout":10}
{"command":["C:\\Windows\\System32\\whoami.exe"],"timeout":10}
```

Linux: call `run_command` with each of these arguments:

```json
{"command":["/bin/hostname"],"timeout":10}
{"command":["/usr/bin/id"],"timeout":10}
{"command":["/usr/bin/whoami"],"timeout":10}
```

Read the actual structured results: completed status, exit code 0, the expected host/account, and stdout/stderr. The Windows account is the gateway account; the Linux beta executes approved commands as root. An HTTP 200, “Connected” label, tool-name description or proposed command is not execution evidence.

## 7. Mark setup complete only after success

Retain a sanitized result tied to the actual artifact and connector observation. Status plus successful native commands completes this onboarding check; it does not replace release qualification, refresh/restart testing, file-transfer acceptance or native process-lifecycle tests. If a call fails, preserve and diagnose that transaction before asking the owner to retry pairing. Keep native/public test results separate from actual-account observations.

## Paths and directory listing

The workspace is the starting directory, not a filesystem sandbox. In the default installation, `.` and relative listing paths start there. A deliberately separate Linux listing root remains the relative-path base and confinement boundary even for approved clients; check `listing_base` in status. Each command is independent: running `cd` in one shell command does not change the working directory of later MCP calls. Supply `cwd` on each command that needs another location; `cd` is a shell builtin, not a persistent MCP session setting.

| Platform/version | Before approval or after grant removal | After exact-client approval |
|---|---|---|
| Published Windows b1 and Windows b3 candidate | Listing denied | Explicit absolute paths such as `C:\` or `C:\Windows` use gateway-account permissions |
| Published Linux b2 | Authenticated listing confined to configured root | The same configured-root confinement; approval does not widen b2 listing |
| Linux b3 candidate | Authenticated listing confined to configured root | Explicit absolute paths such as `/` or `/etc` may be listed wherever root can read; relative paths remain confined to the listing base |

Examples for the upcoming approved connection are `list_directory({"path":"/"})` or `list_directory({"path":"/etc"})` on Linux, and `list_directory({"path":"C:\\"})` or `list_directory({"path":"C:\\Windows"})` on Windows. Use `{"path":"."}` to inspect the configured listing base, which is the workspace in the default installation. An explicit path does not override OS permissions or the current client grant.

The upcoming listing implementation reads at most the requested entry limit plus one entry to detect truncation, then sorts only that bounded sample. Linux retains its entry metadata and `limit` argument (default 100, allowed range 1–200); Windows retains name entries and its fixed 100-entry response limit. A truncated result reports `truncated: true`, `total: null` and `total_is_exact: false`; it does not invent a full-directory total. Only a complete scan within the bound reports an exact total. Published b1/b2 have older response shapes, so consult their matching source/guide instead of assuming the new fields exist. This interface is not a recursive filesystem inventory or a general resource quota.

Revocation is checked again on subsequent requests. In the upcoming Linux candidate it removes broad listing access and restores configured-root confinement; on Windows it denies listing. The upcoming authenticated status tool can still explain the caller's permission state. Removal of an administrative grant does not revoke the OAuth credential family, undo completed changes or cancel accepted jobs. Use the platform's protected full-disable/stop procedure to stop all remote access.
