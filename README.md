# Aster Universal MCP

**Self-hosted remote MCP with Claude: separate Windows and Linux technical betas**

Aster Universal MCP gives an owner-approved Claude connection tools to run commands, manage bounded background jobs, and transfer files on a Windows or Linux computer. It uses OAuth client registration, PKCE, short-lived access tokens, owner pairing and an explicit local client allowlist.

The next intended shared release is **`v0.1.0b3`, currently a candidate**, with separate Windows and Linux archives under one tag. The [Linux guide](docs/LINUX.md) targets Ubuntu 24.04 LTS x86_64 with distribution Python 3.12 and manual root gateway/executor startup. Install as a release only when its matching platform acceptance asset identifies the exact archive and records its required gates as passed.

This is a source-based, manually started **privileged administration beta** for technical users. Windows commands inherit the gateway account's permissions; the Linux reference deployment executes as root. Start on a disposable machine, review [the security boundaries](SECURITY.md), and keep a local administrator session available.

**`v0.1.0b3` candidate — qualification and publication pending:** development adds an authenticated `connection_status` tool, actionable approval errors and permission-aware Linux directory listing. These changes require newly frozen and tested platform artifacts before publication. The immutable Windows `v0.1.0b1` and Linux `v0.1.0b2` downloads retain their original behavior; updating this documentation or the repository does not update an installed gateway. Historical acceptance results below apply only to those original artifacts.

## Prepare the Windows v0.1.0b3 candidate

The intended release assets are listed below. `v0.1.0b3` has not yet been qualified/published; this list is not a claim that downloads exist. Candidate testing must use an identifiable frozen package. After release qualification, obtain matching assets from the project's [Releases page](https://github.com/yustein/aster-universal-mcp/releases):

- `aster-universal-mcp-0.1.0b3-windows.zip` — the source package, frozen manifest and exact Windows dependency lists.
- `SHA256SUMS.txt` — checksums for the published assets.
- `windows-beta-0.1.0b3-acceptance.json` — required Windows results and limitations for that exact package.

Follow the [Windows installation and operation guide](docs/WINDOWS.md). It covers online and offline dependencies, HTTPS routing, pairing, approval, testing, restart, disabling access and removal. Install an archive only when its accompanying acceptance record identifies that archive and marks its required gates passed; a development branch or a test result for an older candidate does not establish release acceptance.

The intended Windows b3 test target remains Windows 11 Enterprise Evaluation 26H2 build 26300.9457 with Python 3.11.9 x64, the accepted b1 reference environment. B3 qualification must record its own actual environment and results. Other Windows/Python combinations need their own testing.

## Prepare the Linux v0.1.0b3 candidate

Linux is intended to share the new `v0.1.0b3` tag with Windows while retaining its own archive, manifest/dependency identity and acceptance evidence. These planned assets are not yet a qualified published release:

- `aster-universal-mcp-0.1.0b3-linux.tar.gz` — frozen source, POSIX file modes and target-derived Linux dependency locks.
- The archive's matching candidate manifest and `SHA256SUMS.txt` — exact source commit/tree and file/archive identity.
- `linux-beta-0.1.0b3-acceptance.json` and its test-evidence archive — separately labeled native, portable, public HTTPS and actual-account results for the new Linux package.

Follow the [Linux installation, operation and recovery guide](docs/LINUX.md). It uses a fresh runtime-only venv, protected independent operator, default workspace outside source, root-only executor socket and a functioning systemd manager. Both synchronous commands and jobs use transient systemd services; the gateway and executor themselves are manually started. Automatic startup or reboot survival is outside this candidate's intended scope. Candidate acceptance uses isolated state/listeners; existing live b1/b2 connections remain unchanged until a separately qualified upgrade.

### Historical release and behavior boundaries

| Version | Published scope | Onboarding/listing behavior |
|---|---|---|
| [Windows `v0.1.0b1`](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b1) | Accepted Windows artifact, unchanged | No `connection_status`; local approval required; approved absolute-path listing |
| [Linux `v0.1.0b2`](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b2) | Accepted Linux artifact, unchanged | No `connection_status`; listing confined to configured root even after approval |
| `v0.1.0b3` candidate | Intended new Windows and Linux artifacts; qualification/publication pending | New status/approval flow on both; Linux approved explicit absolute-path listing; bounded listing metadata |

## How it connects

Claude reaches remote MCP servers from Anthropic's cloud. Each installation therefore needs its own public HTTPS hostname and an owner-controlled proxy or tunnel forwarding to Aster's loopback listener. Check the [official Claude connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) and your account's current connector capacity before adding a server. Preserve working connections when testing another platform.

Use **Sign in now** and **Register automatically** when adding Aster. The host owner issues a short-lived pairing code locally, completes the browser approval, then approves that exact registered client ID through the local operator helper. Pairing and administrative permission are separate steps.

### Complete onboarding before using the computer

The upcoming candidate requires this full sequence on both platforms:

1. Install the verified candidate, start it and verify the local runtime and HTTPS route.
2. Pair the intended connector through OAuth at your own Aster origin.
3. In a trusted local administrator terminal, inspect the registration's exact client ID and callback/redirect URI.
4. Explicitly approve that exact client using the platform's local helper. Installation, runtime readiness and browser pairing never grant administrator access automatically.
5. From that same connector, call `connection_status` with no arguments. Check its own client identity, approval flags, default workspace and effective listing scope against the local registration.
6. Run separate harmless native hostname and identity calls through MCP and inspect their actual structured results.
7. Mark setup complete only after those calls succeed for the intended machine/account. A browser's “Connected” label or a successful status response is insufficient.

Use the [complete onboarding procedure](docs/ONBOARDING.md) for exact Windows/Linux commands, result checks and approval-error recovery. Published b1/b2 do not provide `connection_status`; their platform guides retain the local grant check and actual native-call verification needed for those installed versions.

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

The [Linux technical beta](docs/LINUX.md) runs a root OAuth gateway and a separate root Unix-socket executor. It requires a functioning systemd manager for command/job containment even when both long-running processes are started manually. Published Linux b2 confines authenticated listing to its configured root for all clients. In the upcoming candidate, an approved client can list explicit absolute paths such as `/` or `/etc` wherever root can read; an unapproved or revoked client remains confined to the configured listing root. Windows listing requires approval and accepts absolute paths such as `C:\`. Neither platform's workspace confines an approved command's filesystem access. The Linux protected operator provides a separate full-disable action.

In the default installation, `.` and relative listing paths start at the workspace. A deliberately separate Linux listing root remains the relative-path base and confinement boundary even after approval; the new status tool reports `listing_base` separately from `default_workspace`. Each command has its own working directory: a `cd` in one command does not change the next MCP call. Listings return bounded entries; an entry count is not a complete directory total unless the response explicitly establishes that. See the platform guides for version-specific limits. Windows acceptance does not qualify Linux. Native macOS, startup after reboot, long-duration reliability and large-transfer/load behavior require separate evidence.

## Evidence

The following results are historical acceptance for the named published archives. They do not qualify `v0.1.0b3`; its exact frozen artifact identities, results and publication remain pending.

The published Windows `0.1.0b1` candidate passed 83 native tests plus 7 subtests, a fresh runtime-only application installation and 26 controlled public HTTPS stages. A separate owner-supplied Claude screenshot showed successful `hostname.exe` and `whoami.exe` results on that final candidate. Its Linux-hosted regression run passed 73 tests plus 7 subtests with 10 Windows-only skips; those results do not qualify a Linux deployment. See the [Windows acceptance asset](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b1/windows-beta-0.1.0b1-acceptance.json).

Linux `0.1.0b2` was qualified on 24 October 2026 for Ubuntu 24.04 LTS x86_64, distribution Python 3.12 and manual gateway/executor operation with systemd. Its exact candidate passed 107 native tests plus 7 subtests with 10 Windows-only skips, 94 portable tests plus 7 subtests with 23 skips, 10 fresh-install/recovery stages and 24 controlled public HTTPS stages. These layers overlap and are not added. The owner separately reported successful hostname, id and whoami calls from the actual Claude connection; retained server output spools and audit/job metadata corroborate those results. No raw Claude transcript, success screenshot or exact OAuth-client-to-executor-event mapping is claimed. See [release and acceptance status](docs/RELEASE_STATUS.md) and the matching release acceptance asset for identity, cleanup and evidence limits.

## Open source and voluntary support

Copyright 2026 Aster Universal MCP contributors. The [Apache License 2.0](LICENSE) allows personal and commercial use, modification and redistribution. Donations are voluntary; there is no required paid license or activation. Optional installation or customization assistance can be offered separately.

This project is independent of Anthropic, Claude, OpenAI and the MCP maintainers. It does not bundle a telemetry or activation subsystem. Commands, outputs and file content requested through the connector can be sent to the connected AI service.
