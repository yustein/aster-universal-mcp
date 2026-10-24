# Release and acceptance status

## Published onboarding beta: 0.1.0b3, Windows and Linux

The shared [v0.1.0b3 prerelease](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b3), dated 24 October 2026, contains separate Windows and Linux archives. The exact artifacts passed their native, fresh-install, controlled public HTTPS and final reconciliation gates. **The owner explicitly waived repeating actual Claude-account testing for this update.** This is a recorded release exception, not a passed account observation. No fresh b3 account test, transcript, screenshot or account-to-executor correlation is claimed. Historical b1/b2 account observations remain attached to those earlier artifacts.

The lightweight tag and both source archives identify tested commit **`6d792d0920875bf3cb958b83744cca7a5fc1ec1e`**, tree **`3e4e631f3c0c56e8f6c0fa92f43da67a0bf67615`**, with 103 source files per platform. The implementation merge retains that tested tree. Documentation-only follow-up commits on main do not move the tag, replace either archive or create a new runtime candidate. Packaged candidate documentation was frozen before qualification; use the separately published [release/operator/onboarding guide](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/ASTER_0.1.0b3_RELEASE_OPERATOR_ONBOARDING_GUIDE.md), [release notes](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/RELEASE_NOTES_0.1.0b3.md) and matching acceptance assets for the final result.

| Platform | Manifest SHA-256 | Archive SHA-256 | Archive bytes |
|---|---|---|---:|
| Linux | `047866161b5a91b36799920fec35371a5b4dbc38522768f5801c36e3a128c6c0` | `41f7adc26cbb86b7f9d901fc537b94e59af73f116937f3acadf8003727758c74` | 154,964 |
| Windows | `d673524d12d4543468a7c16f8a21c0e690689871674b16f1b118a6b4d7eb773d` | `2863b818010792711f45c47dbd817da3dc4c475e0ec625c080c6d568e3185d4a` | 201,112 |

The archives are `aster-universal-mcp-0.1.0b3-linux.tar.gz` and `aster-universal-mcp-0.1.0b3-windows.zip`. Their standalone manifest assets are `linux-candidate-manifest.json` and `windows-candidate-manifest.json`; each archive embeds its own manifest as `candidate-manifest.json`. [SHA256SUMS.txt](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/SHA256SUMS.txt) covers the other 20 release assets. [ASSET_MANIFEST.json](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/ASSET_MANIFEST.json) records the 19 assets other than itself and the checksum file.

| Evidence layer | Linux b3 | Windows b3 |
|---|---|---|
| Frozen native suite | 151 tests plus 7 subtests passed; 10 skipped | 126 tests plus 7 subtests passed; 5 skipped |
| Fresh runtime-only installation | Passed with new isolated state and verified dependencies before test packages could mask imports | Passed with new isolated state and verified dependencies before test packages could mask imports |
| Controlled local HTTP endpoint | 14 stages passed | 11 stages passed before an external stopped-listener observer failed; original attempt remains partial/failed |
| Controlled public HTTPS | 25 stages passed | 38 stages passed, including the earlier attempt's remaining manual restart, revoked-token denial and cleanup cases |
| Exact transfer observed over public MCP | 41,031 bytes, three chunks in each direction, exact SHA-256 recovery | 38,054 bytes, three chunks of at most 16,384 bytes, exact SHA-256 recovery and independent native hash |
| Actual Claude account | Repeat test explicitly waived by owner for this artifact | Same explicit artifact-bound waiver |
| Final reconciliation | Source/configuration, venv, both process identities/endpoints and acceptance-owned state verified; historical deployment preserved | Source/configuration, runtime dependencies, process creation/listener and retained ungranted native fixture verified; historical deployment preserved |
| Publication | All 21 shared-release assets downloaded anonymously and hash-verified; all 16 historical b1/b2 asset bytes unchanged | Same shared publication observation; do not count twice |

The primary evidence is [Linux acceptance](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/linux-beta-0.1.0b3-acceptance.json), [Windows acceptance](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/windows-beta-0.1.0b3-acceptance.json), and the separate [Linux](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/linux-beta-0.1.0b3-test-evidence.zip) and [Windows](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/windows-beta-0.1.0b3-test-evidence.zip) sanitized evidence archives. [B3_SANITIZED_EVIDENCE_INDEX.json](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/B3_SANITIZED_EVIDENCE_INDEX.json) records provenance and exclusions. Qualification is `qualified_with_owner_waiver`: release requirements are satisfied, while `all_required_qualification_gates_passed` remains false because the account test was waived. Evidence and notes assembled before publication retain their assembly-time publication status; the final release body and anonymous verification receipt record completed publication.

The Windows local observer failure did not establish listener absence or complete its planned restart/revocation stages. A later independent native listener-table/creation-identity observation reconciled the original stop and fixture state, and the same-candidate public run covered the missing cases. The failed attempt is retained and never relabeled as a full pass. Unknown or later owner state is preserved; test cleanup is limited to identified fixtures. A successful HTTP response or a connect timeout is insufficient evidence of process state.

The shared development run passed 138 tests plus 7 subtests with 23 skips. It is development context, not an additional frozen-artifact acceptance run. Native suites, public stages, shared tests and historical release results overlap; their counts must not be summed. Windows venv evidence verifies the interpreter/prefix and locked runtime packages; Linux additionally verifies its recorded full venv byte/mode/link inventory. Neither statement expands the other platform's guarantees.

The Linux b3 reference guest reported Ubuntu 24.04.5 LTS x86_64, kernel 6.8.0-147-generic, Python 3.12.3 and systemd 255.4-1ubuntu8.18 with cgroup v2. Windows b3 reported Windows 11 Enterprise Evaluation build 26300, Python 3.11.9 x64 and PowerShell 5.1.26100.9444, with manual SYSTEM operation. The target-specific dependency records are `linux-dependency-record.json` and `windows-dependency-record.json`; runtime/test locks remain separate. These dated observations do not qualify other distros, Windows builds, architectures, Python versions, WSL/containers or macOS.

B3 makes local administrator approval explicit in browser onboarding, adds authenticated own-client `connection_status`, and returns actionable approval errors. Pairing does not grant administrator access. **Every new installation still requires exact local client approval, approved status and successful native hostname/identity calls before setup is complete.** The release owner's waiver does not waive these installation checks or prove a new account's connector capacity.

Approved Linux clients may list explicit absolute paths such as `/` or `/etc` wherever root can read. Relative paths remain confined to the fixed listing base; revocation restores confinement for subsequent requests. Windows listing remains approval-gated and supports absolute paths such as `C:\`. `.` uses the configured listing base, and `cd` in one command does not persist into later calls. Bounded listings return an unknown total when truncated. See [ONBOARDING.md](ONBOARDING.md) for the full permission/version matrix.

The immutable Windows `v0.1.0b1` and Linux `v0.1.0b2` tags, releases and assets remain preserved. The sections below retain their original artifact-specific results.

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
| Actual Claude account | Normally a separate final-candidate connection with harmless hostname/identity commands, current account-capacity check and honest evidence limits. B3's repeat test was explicitly waived by the owner for the exact artifacts; record the exception separately and do not mark the observation passed |
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

Use an unused prerelease tag for new runtime bytes. A shared tag may contain separate qualified Windows/Linux artifacts with distinct acceptance and dependency records, as b3 does; never replace `v0.1.0b1`, `v0.1.0b2` or their assets. Before publishing, verify the tag points to the tested source and the merged tree matches it, record all asset hashes, and inspect the completed acceptance record and any explicit owner-authorized exception. After publication, download every asset without authentication and compare its actual bytes, and recheck preserved historical assets. Setup does not publish source, create tunnels, change firewall rules or approve clients.

The project remains Apache-2.0 with voluntary donations and no required paid license or activation.
