# Release and acceptance status

## Published Windows technical beta: 0.1.0b1

[Windows `v0.1.0b1`](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b1) was published on 24 October 2026 as a source-based technical beta with manual elevated operation. Its accepted source/tag commit is `d15d1c4c0fa1d77552bb39990c47b16ec18f9f96`; the merge commit is `b7e255ae2f59cbcfe3937eca3cf7594a3e24c6c3`. Both have source tree `45ecddef5126ebac72ba01e863f98fb966a32acb`.

The accepted manifest SHA-256 is `9d964aa0f01014de5032c28967c07475f1901281d30075329406e34875e1c691`. The Windows source ZIP SHA-256 is `9f91b21669662724edebee37499f92933edbe0d329ef98363e88f60732ad0a17`. The exact public results are in [windows-beta-0.1.0b1-acceptance.json](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b1/windows-beta-0.1.0b1-acceptance.json).

| Evidence layer | Final Windows candidate result |
|---|---|
| Native Windows suite | 83 tests plus 7 subtests passed |
| Portable/shared regression on Linux | 73 tests plus 7 subtests passed; 10 Windows-only skips |
| Fresh application install | New venv and empty isolated application state; runtime-only dependencies on an already provisioned Windows OS |
| Controlled public HTTPS | 26 stages passed, including native effects, refresh, cancellation, manual restart, removal of the controlled client's grant and cleanup |
| Actual Claude account | Separate owner-supplied screenshot of successful hostname and identity commands on the final candidate |
| Publication | Six public assets downloaded without authentication and verified against their recorded hashes |

The reference system was Windows 11 Enterprise Evaluation 26H2 build 26300.9457, Python 3.11.9 x64, manually launched with SYSTEM privileges. The account screenshot contains Claude-rendered result text; full request UUIDs are cropped and no raw MCP transcript or exact full-UUID server correlation is claimed. Earlier RC2 results and failed candidates remain historical evidence, not additional final test counts. Windows `v0.1.0b1` and its published bytes remain unchanged by the separate Linux work.

## Linux technical beta: 0.1.0b2

The Linux artifact's qualification is recorded in its separate `linux-beta-0.1.0b2-acceptance.json` and test-evidence release assets. Those files are written after the source archive is frozen, so evidence does not change the candidate being tested. If the acceptance asset is absent, incomplete or identifies different bytes, do not treat that artifact as qualified.

The reference scope is Ubuntu 24.04 LTS x86_64, distribution Python 3.12, a functioning system systemd manager with cgroup v2, and manual operation of a root OAuth gateway plus a separate root Unix-socket executor. The artifact is `aster-universal-mcp-0.1.0b2-linux.tar.gz`, preserving Linux file modes. Runtime dependencies use `requirements-linux-lock.txt`; test dependencies use the separate `requirements-linux-test-lock.txt`, both resolved on the reference target.

The reference VM inventory at development start was Ubuntu 24.04.5 LTS, kernel 6.8.0-147-generic, Python 3.12.3 and systemd 255 (255.4-1ubuntu8.18). The acceptance asset records the actual final installation versions, UTC timestamps and results. These versions do not qualify other distros, releases, WSL, containers, architectures or Python versions.

| Identity or evidence | Authoritative record |
|---|---|
| Tested source commit/tree and file modes | `candidate-manifest.json`, Git tag and acceptance JSON |
| Manifest and archive SHA-256 | `SHA256SUMS.txt` and acceptance JSON |
| Fresh runtime-only installation | Fresh-install record with new venv/state and dependency inventory |
| Portable/shared and native Linux tests | Separately labeled logs/JUnit and acceptance JSON |
| Actual HTTP/TLS and public HTTPS | Controlled-client acceptance JSON and included harness hash |
| Actual Claude account | Separate account observation with its evidence limitations |
| Cleanup and final source/artifact identity | Final reconciliation and acceptance record |
| Published assets | Public release, exact tag relationship and anonymous download verification |

A version string, branch name, source inspection, package build or successful test of an older candidate is insufficient. Earlier development runs remain separate evidence and their counts must not be added to final-candidate totals.

## Linux release gates

| Gate | Required observation on the candidate |
|---|---|
| Fresh runtime-only application install | Exact archive installed into a new venv and empty isolated state; target-derived lock, dependency checks, source manifest, protected owners/modes and canonical paths verified before test packages are added |
| Both running processes | Root gateway and executor bound to tested source, venv, config, OAuth DB, allowlist, workspace, Unix socket and IPv4 loopback listener; executable, UID/GID and boot/start identities recorded |
| Local authorization boundary | Ordinary user cannot connect to the production executor socket; isolated native test separately verifies rejection of unauthorized peer credentials |
| Independent operator recovery | Status, grant removal and identified shutdown work when application source or venv is unavailable; new starts and approvals reject unexpected source/configuration state |
| Real HTTP OAuth | Discovery, dynamic registration, browser cookie/nonce binding, wrong-code retry, owner approval, PKCE and MCP initialization over actual HTTP/TLS |
| Public HTTPS and exact-client approval | Same verified Linux deployment reached through its own HTTPS route; side effect denied before local approval and observed after exact-client approval |
| Native commands and defaults | Hostname and identity with structured exit/stdout/stderr; omitted cwd for commands and jobs uses ordinary workspace; unique write/read/delete effect independently checked; source unchanged |
| Renewal and binding negatives | Refresh rotation and replay rejection followed by renewed-token command execution; wrong client/scope/resource/expiry covered; invalid exchanges preserve a valid grant; artificial expiry uses isolated DBs |
| Linux transfer | Small file spanning multiple 16 KiB chunks with exact size/hash recovery; wrong offset/hash, incomplete upload and existing-target refusal; protected staging and cleanup checked |
| Native job lifecycle | Successful completion, bounded timeout, worker/child cancellation, failure and interrupted-state reconciliation through functioning systemd; actual process/file effects plus identity-reuse and state-write-race checks |
| Restart and permission continuity | Manual stop/start preserves the controlled client and approval; operation succeeds afterward; grant removal denies privileged operations with existing and renewed tokens; documented listing policy verified |
| Actual Claude account | Separate final-candidate connection with harmless hostname/identity commands; account capacity checked before changing an existing connection; honest screenshots/metadata with stated correlation limits |
| Cleanup and regression | Only acceptance-owned files/clients/jobs removed; every uncertain mutation reconciled; exact source/artifact rechecked; relevant Windows regression/native gates cover any shared-code risks |
| Publication | Exact tested source/tag/tree relationship, manifest, dependencies, checksums, sanitized acceptance JSON, test evidence and operator/recovery guide; every public asset downloaded anonymously and hashed |

## Evidence discipline

Keep four layers separate: portable/shared-code regression, native Linux deployment tests, controlled public HTTPS tests, and actual Claude-account observations. QGA or SSH is the control and independent-verification channel; it does not substitute for native operations through MCP. HTTP 200 can carry a tool error: verify the structured result and the actual effect. A timeout or cancellation status alone does not prove that a child exited.

The acceptance JSON is written outside the frozen source after testing. It identifies source commit/tree, manifest and artifact SHA-256, the reference environment and dependency record, UTC start/finish times, harness identity, stage results and cleanup/reconciliation. A source change produces a new candidate and preserves the failed or superseded run. Keep test functions and subtests distinguishable and never combine overlapping candidate counts.

Journal a mutation before sending it, give it a deadline, and reconcile an uncertain original operation before repeating it. A remote-tool timeout is not proof that the underlying operation failed. Keep a pristine rollback copy and separate code, working files, logs and acceptance evidence.

When an account screenshot is the evidence, state which results are visible, how its time was established, and whether complete request IDs or raw tool messages exist. Do not infer exact command correlation from approximate timestamps or token-renewal metadata. Public evidence must exclude live owner codes, cookies, access/refresh tokens, token digests, client secrets, credential-bearing database contents and account passwords.

## Scope limits

- Manual startup and an owner-managed HTTPS route are deliberate requirements. Persistent service installation, automatic startup and reboot recovery need separate implementation and acceptance.
- Linux commands and jobs run as root. The default workspace organizes output; it is not a filesystem sandbox. Protected state and its workspace are not intended for ordinary unprivileged local-user access.
- Per-stream response and captured-output limits do not impose general CPU, memory, filesystem or total retained-job quotas. Claims about process-tree termination must match native cgroup evidence.
- Removing an administrator allowlist entry blocks privileged Linux tools but leaves authenticated listing inside its configured root. Full-disable, grant removal and OAuth credential-family revocation are different operations.
- File sizes, concurrency, elapsed durations and workloads are qualified only to the extent recorded in the matching acceptance asset. A declared transfer-size maximum is not a tested-size claim.
- No native macOS, broad distro/architecture coverage, long-duration reliability, sustained-load qualification, signed binary installer or independent security audit is implied.

## Publication rules

Linux requires a separate unused prerelease tag; never replace Windows `v0.1.0b1` or its assets. Before publishing, verify the tag points to the tested source and the merged tree matches it, record all asset hashes, and inspect the completed acceptance record. After publication, download every asset without authentication and compare its actual bytes. Setup does not publish source, create tunnels, change firewall rules or approve clients.

The project remains Apache-2.0 with voluntary donations and no required paid license or activation.
