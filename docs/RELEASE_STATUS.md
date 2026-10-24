# Release and acceptance status

## Onboarding update candidate: 0.1.0b3, Windows and Linux

This source prepares separate Windows and Linux `0.1.0b3` archives. Qualification
and publication must be established by the matching platform acceptance records;
the completed b1/b2 results below do not qualify new runtime bytes.

The candidate makes local administrator approval explicit in browser onboarding
and installation instructions, adds an authenticated own-client
`connection_status` tool, and returns actionable expected-denial messages.
Pairing still does not grant administrator access. Setup is complete only after
the exact client is locally approved and native hostname/identity calls succeed.

Linux's listing policy changes: an approved client can request explicit absolute
paths outside the configured listing root. Relative paths remain confined to the
listing base, and revocation restores restricted listing on subsequent calls.
Windows listing remains approval-gated and supports absolute paths. Both platform
listings bound enumeration and identify an unknown total when results truncate.
See [ONBOARDING.md](ONBOARDING.md) for the version and permission matrix.

Retain the exact source commit, each platform's distinct manifest/archive hash,
native Windows/Linux and shared OAuth results, fresh runtime-only installation,
controlled endpoint observations, and any separately obtained actual-account
observations. A status response alone does not establish execution health. Keep
the published `v0.1.0b1` and `v0.1.0b2` tags and asset bytes unchanged.

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

## Published Linux technical beta: 0.1.0b2

[Linux `v0.1.0b2`](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b2) was published on 24 October 2026 for Ubuntu 24.04 LTS x86_64, distribution Python 3.12, a functioning system systemd manager with cgroup v2, and manual gateway/executor startup. The tested source commit is `c2ec9ce1d258d80c4922c190212ff5681f05c128`, source tree `8e5f7d653c1803d286c9295fb89b61d6c570ca44`, manifest SHA-256 `c6dc45c5d2d73ad8e74ab43c92849fe8c9a9cc8003edbf11fdc863c3d81be1b4`, and source archive SHA-256 `f879eccf69aec995eaccfb14cc895cb3bdd762bdca190b8e4875b1e1c77e3887` (133,177 bytes).

The release tag identifies that tested source commit. The recorded main-branch merge is `f440cacd156de931b70304f0793492c85ce849ce`, with the same source tree. Later documentation commits on main do not replace the tested commit or its archive.

| Evidence layer | Final Linux candidate result |
|---|---|
| Portable regression | 94 tests plus 7 subtests passed; 23 skips; exact source/archive receipt and clean inventory |
| Native Linux | 107 tests plus 7 subtests passed; 10 Windows-only skips; source unchanged |
| Fresh runtime-only installation/recovery | 10 passed stages; independent operator recovered with application source and venv unavailable |
| Controlled public HTTPS | 24 passed stages; native effects, 41,031-byte exact transfer, process-tree lifecycle, refresh, manual restart, removed-grant semantics and cleanup |
| Actual Claude account | Three owner-reported separate hostname/id/whoami successes, cwd omitted, exit 0; matching retained server stdout spools and audit/job joins corroborate activity on the unchanged candidate |
| Final cleanup | Three identified acceptance jobs preserved as private evidence; two later unrelated completed jobs and the intended real client approval preserved; no pending lifecycle operation |
| Publication | All 10 Linux release assets downloaded without authentication and hash-verified; all six Windows asset bytes unchanged. The release body and project handoff record the tag/source/tree relationship and verification details |

Actual-account success evidence consists of owner-supplied text and server records, without a success screenshot or raw Claude protocol transcript. Stored stdout for the three matched results is 18, 39 and 5 bytes including newline, with zero stderr. Executor records retain no original argv/cwd or OAuth client identity, so exact Claude invocation/client UUID correlation is not claimed. Approved commands may use any existing absolute cwd, including `/`; only `list_directory` is confined to its configured root, independently of the admin allowlist. The successful omitted-cwd calls do not establish an identical replay of earlier calls with other arguments. No actual-account transfer or file-write test is claimed.

The fresh reference guest reported Ubuntu 24.04.5 LTS, kernel 6.8.0-147-generic, Python 3.12.3 and systemd 255.4-1ubuntu8.18. Test counts overlap and are not summed. Earlier failed harness attempts and their diagnosis remain retained. These dated results concern the exact frozen artifact; later documentation corrections on main do not create a new runtime candidate or change the release tag/archive.

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
