# Aster Universal MCP

**Full remote MCP beta for Claude Free — Windows and Linux**

Aster Universal MCP is a free and open-source, self-hosted MCP server supporting **OAuth 2.0 client registration, PKCE owner pairing, permission-controlled command execution, background jobs, cancellation, and chunked file transfer** on Windows and Linux.

**Important:** This is a *privileged administration* beta. An authorized Claude connector can initiate commands and potentially modify host files. It is not a read-only demo. Test on a disposable machine and read [SECURITY.md](SECURITY.md) before connecting an AI client.

## Claude Free

Anthropic currently supports **one custom remote MCP connector on Claude Free**. The connector is established at **Customize → Connectors → Add → Custom → Web** and uses a publicly reachable HTTPS MCP endpoint, such as `https://mcp.example.com/mcp`.

The connection originates in Anthropic's cloud. The beta therefore requires a domain plus a secure reverse proxy or tunnel to the local Aster OAuth gateway. **A local `claude_desktop_config.json` file alone is not the Claude Free custom remote connector.**

Choose **Register automatically** for the OAuth client, then complete the owner-pairing flow. The owner must *also* explicitly authorize the registered client's ID for administrative execution. No tool is anonymously executable.

## Platform capabilities

| Capability | Linux | Windows |
|---|---|---|
| OAuth 2.0 / PKCE / owner pairing | Implemented | Implemented |
| Remote Streamable HTTP MCP endpoint (behind HTTPS) | Localhost 8767 | Localhost 18767 |
| Command execution | Local root-only administrator executor | Authorized gateway process identity |
| Background jobs / status / cancellation | Persistent local job manager | Verified worker identity + Windows Job Objects |
| File upload / download | SHA-256-verified chunked transfer | SHA-256-verified chunked transfer |
| Access control | OAuth `system:admin` + owner-managed client allowlist | OAuth `system:admin` + owner-managed client allowlist |
| Service deployment | Manual systemd setup | Manual console launch or administrator-managed service |

**Status:** Aster's complete remote OAuth integration with Claude Free must pass a real Claude account connection before claiming Claude interoperability is accepted. OAuth interoperability and platform tests are not a substitute for that account-level test.

## Setup

- [Linux (Ubuntu/Debian): OAuth, root executor, HTTPS](docs/LINUX.md)
- [Windows: OAuth, privileged gateway, HTTPS](docs/WINDOWS.md)
- [Security boundaries](SECURITY.md)
- [Operators and release status](docs/RELEASE_STATUS.md)

Python 3.11+ is required. Start the gateway only on localhost, in a Python virtual environment. The installer is intentionally source-based for this beta; there is no mandatory paid license, activation, or one-click installer.

## Authentication and owner approval

When Claude initiates OAuth, Aster creates a pending pairing request and displays an authorization page. The host owner locally issues a short-lived pairing code and enters it in that page. The OAuth server issues a token scoped to `system:admin` only after successful pairing and PKCE exchange.

**Administrative execution needs a second explicit step:** on the host, the owner inspects registered OAuth clients and adds the intended client ID to a local protected allowlist using `admin_client_allowlist.py`. Unknown, unpaired, revoked, and non-allowlisted clients cannot invoke privileged tools.

The approval CLI is never exposed as an MCP tool. The OAuth server must not be accessible except through the configured HTTPS endpoint and owner-controlled host policy.

## Open source

Copyright 2026 Aster Universal MCP contributors. [Apache License 2.0](LICENSE) grants free personal and commercial use, modification, and redistribution. Donations, if offered, are voluntary. Optional setup or customization assistance can be offered separately; it is never required to use the software.

This project is independent of Anthropic, Claude, OpenAI, and the MCP maintainers. No GitHub Actions, telemetry, paid hosting, or activation subsystem is bundled.

## Beta limitation

A fresh-machine Windows source build completed 118 Windows tests and 134 core tests (one skipped) in the engineering project before this release branch was isolated. This new *full remote OAuth* distribution requires its own end-to-end OAuth/privileged execution validation on **both** Windows and Linux before public release. Do not treat preliminary security unit-test results as equivalent to that final acceptance.

Relevant official reference: [Claude custom remote MCP connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).


### Pairing codes and authorization deadlines

Pairing codes have 12 characters (formatted `XXXX-XXXX-XXXX`) using an unambiguous 32-symbol alphabet. Spaces, hyphens and lowercase are accepted. Generating a new code invalidates the prior code.

On Windows, follow the [shared configuration and verified startup workflow](docs/WINDOWS.md) before running `scripts/issue-pairing-windows.ps1`. The helper checks the live gateway and its exact database before issuing a code. Keep the `.cmd` convenience wrapper in the repository's `scripts` directory.

The owner code can remain valid for 15 minutes, but the current OAuth authorization request and browser session have their own five-minute lifetime. A fresh code does not extend an old request. After a failed or expired authorization, inspect the failure and begin a fresh request before retrying.
