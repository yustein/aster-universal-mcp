# Aster Universal MCP

**Windows technical beta for self-hosted remote MCP with Claude**

Aster Universal MCP gives an owner-approved Claude connection tools to run commands, manage bounded background jobs, and transfer files on a Windows computer. It uses OAuth client registration, PKCE, short-lived access tokens, owner pairing and an explicit local client allowlist.

This is a source-based, manually started **privileged administration beta** for technical users. Commands inherit the Windows gateway account's permissions. Start on a disposable machine, review [the security boundaries](SECURITY.md), and keep a local administrator session available.

## Get the Windows beta

Use the matching assets from the project's [Releases page](https://github.com/yustein/aster-universal-mcp/releases):

- `aster-universal-mcp-0.1.0b1-windows.zip` — the source package, frozen manifest and exact Windows dependency lists.
- `SHA256SUMS.txt` — checksums for the published assets.
- `windows-beta-0.1.0b1-acceptance.json` — results and limitations for that exact package.

Follow the [Windows installation and operation guide](docs/WINDOWS.md). It covers online and offline dependencies, HTTPS routing, pairing, approval, testing, restart, disabling access and removal. Install an archive only when its accompanying acceptance record identifies that archive and marks its required gates passed; a development branch or a test result for an older candidate does not establish release acceptance.

The reference test environment is Windows 11 Enterprise Evaluation 26H2 build 26300.9457 with Python 3.11.9 x64. The version-specific acceptance asset states the environment actually used for the released package. Other Windows/Python combinations need their own testing.

## How it connects

Claude reaches remote MCP servers from Anthropic's cloud. Each installation therefore needs its own public HTTPS hostname and an owner-controlled proxy or tunnel forwarding to Aster's loopback listener. The [official Claude connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) documents current account and network requirements; Claude Free currently permits one custom connector.

Use **Sign in now** and **Register automatically** when adding Aster. The host owner issues a short-lived pairing code locally, completes the browser approval, then approves that exact registered client ID through the local operator helper. Pairing and administrative permission are separate steps.

## Capabilities and scope

| Capability | Windows technical beta |
|---|---|
| Authentication | OAuth registration, PKCE, owner pairing, rotating refresh tokens |
| Remote MCP transport | Streamable HTTP on `127.0.0.1:18767`, behind the owner's HTTPS route |
| Command execution | The gateway process account's privileges |
| Working files | A dedicated workspace outside the frozen application source; explicit paths are supported |
| Background work | Bounded jobs, status and cancellation |
| File transfer | Chunked upload/download with size and SHA-256 checks |
| Administrative permission | `system:admin` OAuth scope plus an exact client ID in the local allowlist |
| Local recovery | Protected operator commands to revoke, disable access and stop the verified gateway |
| Deployment | Source archive, repository-local Python environment, manual foreground or background launch |

Linux implementation and [engineering instructions](docs/LINUX.md) remain available, but this Windows release does not establish fresh-machine Linux account acceptance. Native macOS acceptance is outside this release. Service installation, startup after reboot, long-duration reliability and large-transfer/load qualification are not implied by a passing smoke test.

## Evidence

The preceding Windows RC2 candidate passed 54 native tests and 19 controlled public HTTPS acceptance/cleanup stages. An actual Claude account also connected and returned successful `hostname.exe` and `whoami.exe` results. Those are recorded as prior-candidate evidence, with the account smoke test distinguished from the broader controlled protocol tests.

The follow-up candidate addresses the default-workspace and operator-recovery issue found during that test and adds refresh-exchange coverage. Read the release's accompanying acceptance asset for its own results; see [release and acceptance status](docs/RELEASE_STATUS.md) for the evidence policy and remaining scope.

## Open source and voluntary support

Copyright 2026 Aster Universal MCP contributors. The [Apache License 2.0](LICENSE) allows personal and commercial use, modification and redistribution. Donations are voluntary; there is no required paid license or activation. Optional installation or customization assistance can be offered separately.

This project is independent of Anthropic, Claude, OpenAI and the MCP maintainers. It does not bundle a telemetry or activation subsystem. Commands, outputs and file content requested through the connector can be sent to the connected AI service.
