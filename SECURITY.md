# Security boundaries — privileged Windows technical beta

Aster deliberately gives an approved remote MCP client powerful host-execution and file-access tools. Install it only on a machine you own and control, begin on a disposable machine, and retain local administrator access. The gateway account determines the available Windows privileges; the acceptance VM used SYSTEM, but the package does not require every installation to use that account.

## Network and authorization

- The Windows gateway binds to `127.0.0.1:18767`. Use an owner-controlled HTTPS proxy or tunnel that preserves the configured public Host and supplies `X-Forwarded-Proto: https`. Do not publish the raw gateway port directly.
- Owner pairing, an OAuth token scoped to `system:admin`, and an exact local client-allowlist entry are all required for privileged tools. Administrative approval is a separate local action after browser pairing.
- Access tokens are short-lived; refresh tokens rotate. Protect the OAuth database and browser approval material as credentials. The public OAuth revocation endpoint is not enabled in this beta; use the documented local operator commands to remove administrative permission.
- There is no default shared administrator password or wildcard client grant. Keep tunnel credentials outside source and outside any release archive.
- Requests, outputs and file content used through MCP can reach the connected AI service. Restrict beta work to data appropriate for that service and review requested actions.

## Source, workspace and state

The release archive contains a manifest of source-file hashes. Verify the archive's SHA-256 against the trusted release checksum before installing. The manifest checks local byte identity; it does not authenticate an unknown download. Do not regenerate it to dismiss an unexplained source change.

Setup restricts `%ProgramData%\AsterUniversalMCP` to SYSTEM and Administrators. This directory contains configuration, OAuth state, the client allowlist, runtime records, logs and the default workspace. Keep work products in the workspace or another deliberate working directory outside the frozen source. An explicit `cwd` or file path can access other locations permitted by the gateway account; the workspace is an operational default, not a filesystem sandbox.

Startup, owner-code issuance and new administrative approval require a verified candidate and deployment. Verified setup also installs an independent operator script in the protected state directory. That script can remove grants and stop the identified gateway even if the source inventory later changes; it does not import repository Python code. Use the protected path in the [Windows recovery instructions](docs/WINDOWS.md#disable-access-and-stop).

## Disabling access

Revoke the host allowlist entry when disconnecting a client. Removing a connector in Claude does not clear the host's persisted grants or OAuth state.

The protected operator's `revoke` action removes one client's administrative grant. `disable` removes all grants; `disable -StopGateway` also stops the verified gateway. `stop` preserves grants and state for a later manual restart. If process identity cannot be established, the helper must refuse to terminate an unrelated process. Disable the dedicated public route and inspect the local process identity before manual intervention.

Revoking or stopping prevents new privileged work but does not undo completed actions or automatically cancel all already-running jobs and child processes. Cancel and verify known jobs before a planned stop. During an incident, first disable access, then inspect outstanding work locally. Preserve relevant logs without copying credential values into reports.

## Beta limits and vulnerability reports

Manual startup, explicit approval and owner-managed networking are the supported beta operating model. Service/autostart behavior, sustained load and untested host/AI-client combinations require separate qualification. The beta does not claim an independent security audit, a signed binary installer or freedom from filesystem race conditions.

Linux engineering code uses a separate root-only executor socket and its own [deployment guide](docs/LINUX.md). The Windows release acceptance does not qualify that deployment or macOS.

Report vulnerabilities privately to the maintainer using GitHub private vulnerability reporting when enabled, or another established private channel. Include the affected version, operating system, reproduction steps and redacted logs. Do not post live access tokens, pairing codes, private data or tunnel credentials in public issues.
