# Aster Universal MCP

**Self-hosted remote MCP with Claude: separate Windows and Linux technical betas**

Aster Universal MCP gives an owner-approved Claude connection tools to run commands, manage bounded background jobs, and transfer files on a Windows or Linux computer. It uses OAuth client registration, PKCE, short-lived access tokens, owner pairing and an explicit local client allowlist.

**[v0.1.0b3](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b3) is the shared Windows and Linux onboarding beta**, with separate archives and acceptance records under one tag. Its exact artifacts passed their native, fresh-install and controlled public HTTPS gates. The owner explicitly waived repeating actual Claude-account testing for this update; no fresh b3 account success is claimed. The [release status](docs/RELEASE_STATUS.md) records this distinction and the supported reference environments.

This is a source-based, manually started **privileged administration beta** for technical users. Windows commands inherit the gateway account's permissions; the Linux reference deployment executes as root. Start on a disposable machine, review [the security boundaries](SECURITY.md), and keep a local administrator session available.

**New in b3:** authenticated `connection_status`, actionable approval errors and permission-aware Linux directory listing. The immutable Windows `v0.1.0b1` and Linux `v0.1.0b2` downloads retain their original behavior; updating documentation or the repository does not update an installed gateway. The b3 source archives and tag remain at tested commit `6d792d0920875bf3cb958b83744cca7a5fc1ec1e`. Their packaged documentation was frozen before qualification; the matching release acceptance assets and [release/operator guide](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/ASTER_0.1.0b3_RELEASE_OPERATOR_ONBOARDING_GUIDE.md) supply the final result without rewriting tested bytes.

## Install the Windows v0.1.0b3 beta

Download the matching [b3 release assets](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b3):

- [aster-universal-mcp-0.1.0b3-windows.zip](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/aster-universal-mcp-0.1.0b3-windows.zip) — source package, frozen manifest and exact Windows dependency lists.
- [SHA256SUMS.txt](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/SHA256SUMS.txt) — checksums for the assets.
- [windows-beta-0.1.0b3-acceptance.json](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/windows-beta-0.1.0b3-acceptance.json) — Windows results, the explicit account-test waiver and limitations for this package.

Follow the [Windows installation and operation guide](docs/WINDOWS.md). It covers online and offline dependencies, HTTPS routing, pairing, approval, testing, restart, disabling access and removal. Check the acceptance record's exact archive identity, satisfied release requirements and explicitly documented exception. An older candidate's test result does not qualify different bytes.

The b3 reference system reported Windows 11 Enterprise Evaluation build 26300 and Python 3.11.9 x64, with manual SYSTEM operation. Other Windows/Python combinations need their own testing.

## Install the Linux v0.1.0b3 beta

Linux shares the `v0.1.0b3` tag while retaining its own archive, manifest, dependency record and native acceptance evidence:

- [aster-universal-mcp-0.1.0b3-linux.tar.gz](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/aster-universal-mcp-0.1.0b3-linux.tar.gz) — frozen source, POSIX file modes and target-derived Linux dependency locks.
- [linux-candidate-manifest.json](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/linux-candidate-manifest.json) and the shared checksum file — exact source commit/tree and file/archive identity.
- [linux-beta-0.1.0b3-acceptance.json](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/linux-beta-0.1.0b3-acceptance.json) and [test evidence](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/linux-beta-0.1.0b3-test-evidence.zip) — separately labeled native/public results, development context and the explicit account-test waiver.

Follow the [Linux installation, operation and recovery guide](docs/LINUX.md) for Ubuntu 24.04 LTS x86_64 and distribution Python 3.12. It uses a fresh runtime-only venv, protected independent operator, workspace outside source, root-only executor socket and a functioning system systemd manager. Both synchronous commands and jobs use transient systemd services; the gateway and executor themselves are manually started. Gateway service/autostart/reboot support is not qualified. Existing live b1/b2 connections must be preserved during any separately verified upgrade.

### Historical release and behavior boundaries

| Version | Published scope | Onboarding/listing behavior |
|---|---|---|
| [Windows `v0.1.0b1`](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b1) | Accepted Windows artifact, unchanged | No `connection_status`; local approval required; approved absolute-path listing |
| [Linux `v0.1.0b2`](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b2) | Accepted Linux artifact, unchanged | No `connection_status`; listing confined to configured root even after approval |
| [Windows and Linux `v0.1.0b3`](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b3) | Native/public gates passed; repeat account test explicitly waived | New status/approval flow on both; Linux approved explicit absolute-path listing; bounded listing metadata |

## How it connects

Claude reaches remote MCP servers from Anthropic's cloud. Each installation therefore needs its own public HTTPS hostname and an owner-controlled proxy or tunnel forwarding to Aster's loopback listener. Check the [official Claude connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) and your account's current connector capacity before adding a server. Preserve working connections when testing another platform.

Use **Sign in now** and **Register automatically** when adding Aster. The host owner issues a short-lived pairing code locally, completes the browser approval, then approves that exact registered client ID through the local operator helper. Pairing and administrative permission are separate steps.

### Complete onboarding before using the computer

Every new b3 installation requires this full sequence on both platforms. The release owner's waiver of repeat account testing does not waive these setup checks:

1. Install the verified release archive, start it and verify the local runtime and HTTPS route.
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

The [Linux technical beta](docs/LINUX.md) runs a root OAuth gateway and a separate root Unix-socket executor. It requires a functioning systemd manager for command/job containment even when both long-running processes are started manually. Historical Linux b2 confines authenticated listing to its configured root for all clients. In b3, an approved client can list explicit absolute paths such as `/` or `/etc` wherever root can read; an unapproved or revoked client remains confined to the configured listing root. Windows listing requires approval and accepts absolute paths such as `C:\`. Neither platform's workspace confines an approved command's filesystem access. The Linux protected operator provides a separate full-disable action.

In the default installation, `.` and relative listing paths start at the workspace. A deliberately separate Linux listing root remains the relative-path base and confinement boundary even after approval; the new status tool reports `listing_base` separately from `default_workspace`. Each command has its own working directory: a `cd` in one command does not change the next MCP call. Listings return bounded entries; an entry count is not a complete directory total unless the response explicitly establishes that. See the platform guides for version-specific limits. Windows acceptance does not qualify Linux. Native macOS, startup after reboot, long-duration reliability and large-transfer/load behavior require separate evidence.

## Evidence

The b3 evidence is specific to its frozen archives. Test layers overlap and must not be added together:

| Evidence layer | Linux b3 | Windows b3 |
|---|---|---|
| Native suite | 151 passed, 10 skipped, 7 subtests passed | 126 passed, 5 skipped, 7 subtests passed |
| Fresh runtime-only installation | Passed | Passed |
| Controlled local HTTP endpoint | 14 stages passed | 11 stages passed before an external stopped-listener observer failed; that attempt remains partial/failed |
| Controlled public HTTPS | 25 stages passed | 38 stages passed; covered the earlier attempt's remaining restart/revocation/cleanup cases |
| Repeat actual Claude-account test | Explicitly waived by the owner for this exact update; no fresh account observation claimed | Same explicit waiver |

The shared development run passed 138 tests plus 7 subtests with 23 skips. It is development context, not an additional frozen-candidate acceptance total. Final source/configuration, lifecycle, fixture and historical-baseline reconciliation is recorded separately. See [release status](docs/RELEASE_STATUS.md) and the matching acceptance assets for hashes, native environments, failure reconciliation and publication verification.

The following results remain historical acceptance for their original archives:

The published Windows `0.1.0b1` candidate passed 83 native tests plus 7 subtests, a fresh runtime-only application installation and 26 controlled public HTTPS stages. A separate owner-supplied Claude screenshot showed successful `hostname.exe` and `whoami.exe` results on that final candidate. Its Linux-hosted regression run passed 73 tests plus 7 subtests with 10 Windows-only skips; those results do not qualify a Linux deployment. See the [Windows acceptance asset](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b1/windows-beta-0.1.0b1-acceptance.json).

Linux `0.1.0b2` was qualified on 24 October 2026 for Ubuntu 24.04 LTS x86_64, distribution Python 3.12 and manual gateway/executor operation with systemd. Its exact candidate passed 107 native tests plus 7 subtests with 10 Windows-only skips, 94 portable tests plus 7 subtests with 23 skips, 10 fresh-install/recovery stages and 24 controlled public HTTPS stages. These layers overlap and are not added. The owner separately reported successful hostname, id and whoami calls from the actual Claude connection; retained server output spools and audit/job metadata corroborate those results. No raw Claude transcript, success screenshot or exact OAuth-client-to-executor-event mapping is claimed. See [release and acceptance status](docs/RELEASE_STATUS.md) and the matching release acceptance asset for identity, cleanup and evidence limits.

## Open source and voluntary support

Copyright 2026 Aster Universal MCP contributors. The [Apache License 2.0](LICENSE) allows personal and commercial use, modification and redistribution. Donations are voluntary; there is no required paid license or activation. Optional installation or customization assistance can be offered separately.

This project is independent of Anthropic, Claude, OpenAI and the MCP maintainers. It does not bundle a telemetry or activation subsystem. Commands, outputs and file content requested through the connector can be sent to the connected AI service.
