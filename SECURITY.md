# Security boundaries — privileged administration beta

Aster deliberately gives an approved remote MCP client powerful host-execution and file-access tools. Install it only on a machine you own and control, begin on a disposable machine, and retain an independent local administrator session. The shared [Windows/Linux `v0.1.0b3` release](https://github.com/yustein/aster-universal-mcp/releases/tag/v0.1.0b3) is a technical beta, with separate exact-artifact acceptance records. Linux qualification is limited to the documented Ubuntu 24.04 LTS x86_64/Python 3.12 manual deployment. [Release status](docs/RELEASE_STATUS.md) separates the evidence layers and retains the original b1/b2 history; this security description is not an independent audit.

Windows commands inherit the gateway account's permissions; the accepted reference VM used SYSTEM. The Linux beta uses a root OAuth gateway and a separate root Unix-socket executor, and its approved commands/jobs run as root. Neither model is a sandbox against an approved administrator or the local root/Administrator account.

**b3 qualification includes an explicit owner waiver of repeat Claude-account testing.** Native, fresh-install, controlled public HTTPS and final reconciliation gates support this update; no fresh b3 account observation, transcript or screenshot is claimed. The waiver concerns release evidence and never authorizes an automatic grant or removes a new installation's required approval/status/native-command checks. The frozen b3 tag/source remains `6d792d0920875bf3cb958b83744cca7a5fc1ec1e`; packaged candidate documentation predates the final result. Consult the [matching release/operator guide](https://github.com/yustein/aster-universal-mcp/releases/download/v0.1.0b3/ASTER_0.1.0b3_RELEASE_OPERATOR_ONBOARDING_GUIDE.md) and platform acceptance assets. Historical b1/b2 archives, tags and evidence remain unchanged.

## Network and authorization

| Boundary | Windows | Linux technical beta |
|---|---|---|
| Gateway | IPv4 loopback, default `127.0.0.1:18767` | IPv4 loopback, canonical setup default `127.0.0.1:18768` |
| Public ingress | Owner-controlled HTTPS proxy/tunnel | Separate owner-controlled HTTPS proxy/tunnel |
| Privileged execution | Gateway account | Root executor over a protected Unix socket |
| Local executor access | Platform-specific Windows implementation | Root-owned mode 0600 socket plus Linux `SO_PEERCRED` root-peer check |
| Remote privileged tools | OAuth `system:admin` scope and exact local client grant | OAuth `system:admin` scope and exact local client grant |
| Directory listing in published b1/b2 | Exact administrative grant required; absolute paths use gateway-account access | OAuth authenticated, confined to configured listing root for every client; independent of the administrator allowlist |
| Directory listing in b3 | Exact administrative grant required; relative paths start at workspace and absolute paths use gateway-account access | Approved explicit absolute paths may use root's access; unapproved clients and all relative paths remain confined to the configured listing root |

The trusted ingress must connect from `127.0.0.1`, preserve the configured public `Host`, and supply `X-Forwarded-Proto: https`. Do not expose the raw gateway listener on a public interface. If the proxy is on another machine, use a locally terminating channel that preserves this boundary; a direct guest-LAN connection is not equivalent to loopback. Setup does not provision ingress or change firewall rules.

Owner browser pairing establishes OAuth authorization. Privileged tools require a separate exact-client administrative grant made locally after verifying that client's identity and registered redirect URI. There is no shared default administrator password or wildcard grant. Protect the OAuth database, its sidecars and browser approval material as credentials; keep tunnel credentials outside source and release archives.

The mandatory [onboarding flow](docs/ONBOARDING.md) is install/start, OAuth pair, inspect the exact client ID and callback in a trusted local administrator terminal, explicitly approve it, check that connector's `connection_status`, then verify harmless native hostname/identity calls. Setup is complete only after actual command success. Runtime-ready, OAuth-connected and client-approved are distinct states. Never approve an ID solely because a remote error message asks for it; compare the locally registered client and intended owner interaction first.

In b3, `connection_status` is authenticated and reports only the calling client's identity/approval state plus its default workspace and listing scope. It does not approve a client, enumerate other clients, issue credentials or prove that a native command ran. Expected permission denial is returned as an actionable MCP tool error (`ToolError`, `ADMIN_APPROVAL_REQUIRED`); an HTTP 200 can still contain a failed tool result. An invalid or unreadable allowlist produces `AUTHORIZATION_STATE_UNAVAILABLE` and needs local diagnosis, not an assumed missing grant. The local owner decides whether to grant access. Missing approval must never trigger automatic root grants or repeated browser pairing as a substitute.

Status and listing still require valid OAuth authentication and the `system:admin` scope. `OAUTH_SCOPE_REQUIRED` is distinct from missing local approval: an allowlist grant cannot add a missing OAuth scope. Diagnose the actual returned error and credential flow rather than applying the same remedy to every denial.

The b3 Linux listing policy checks current administrative permission on each call. Approved clients can list explicit absolute paths anywhere root can read, including `/` and `/etc`; unapproved clients remain confined to the configured listing root. Relative Linux paths remain confined to that root even for approved clients. Revocation restores confinement for subsequent absolute-path calls, including calls with still-valid or renewed tokens. Windows listing already requires approval and can reach explicit absolute paths accessible to the gateway account. Bounded listing responses are not a directory-size or filesystem resource quota.

Access tokens are short-lived and refresh tokens rotate. The shared resource-binding check rejects a wrong, empty or duplicated explicit token-request resource before consuming an otherwise valid code or refresh grant. Omitted resource remains supported for the deployment's bound audience. The public OAuth revocation endpoint is not enabled, and this beta does not claim access/refresh credential-family tracking or family revocation.

Commands, outputs and file contents requested through MCP can reach the connected AI service. Use data appropriate for that service and review requested operations. OAuth and administrator approval establish permission; they do not make an arbitrary root command safe.

## Source, workspace and protected state

Verify the archive SHA-256 using the trusted release checksum before installing. The source manifest establishes local byte identity; it does not authenticate an unknown download or protect against root deliberately replacing both source and trust records. Do not regenerate the manifest to dismiss unexplained source drift.

Windows setup restricts `%ProgramData%\AsterUniversalMCP` to SYSTEM and Administrators. It contains configuration, OAuth state, allowlist, runtime records, logs and the default workspace. Use the [versioned Windows instructions](docs/WINDOWS.md) for its protected operator and ACL model.

The Linux layout separates root-owned frozen source and venv under `/opt/aster-universal-mcp` from protected operational state under `/var/lib/aster-universal-mcp`. State contains the canonical config, OAuth database, allowlist, independent operator, runtime identities, logs, job/transfer state and workspace. State/control directories and files are protected from ordinary unprivileged local users. Work output belongs in the workspace or another deliberate path outside frozen source. An ordinary local user is not given access to protected state merely to make workspace use convenient.

Commands and jobs that omit `cwd` use the configured workspace. `.` and relative listing paths also start there in the canonical setup. If a Linux operator deliberately configures a separate listing root, it remains the relative-listing base; b3's status tool reports `listing_base` and `default_workspace` separately. Absolute paths are evaluated under the platform/version's listing policy above. A command's `cd` changes only that command process, not the gateway's workspace or later MCP calls. Explicit command paths and transfer paths can reach other locations available to the execution account. Neither the workspace nor unapproved-listing confinement restricts an approved root command's filesystem access.

New starts, owner-code issuance and new administrative approvals require the verified source/configuration state. Each platform installs an independent protected operator so access removal and safe identified shutdown remain available when application code or its venv is missing or damaged. The Linux operator uses distribution Python and its standard library rather than importing application modules. Keep that protected operator, distribution Python and the local administrator channel available. See the [Linux recovery procedure](docs/LINUX.md#recovery-when-source-or-venv-is-unavailable).

Lifecycle operations must identify the actual process, including its start identity, executable and relevant listener/socket, before acting. PID alone is insufficient. If an identity cannot be established, the operator must refuse to terminate an unrelated process; isolate the dedicated public route and inspect local evidence before manual intervention.

## Access removal and full disable

Removing a connector in Claude does not clear persisted host grants or OAuth credentials. Removing a host grant does not undo completed actions or revoke a credential family.

| Operator action | Effect and limit |
|---|---|
| Windows `revoke` | Removes one client's administrative grant, denying subsequent privileged tools including directory listing; b3's authenticated status tool remains available |
| Windows `disable` | Removes all administrative grants; `-StopGateway` also stops the verified gateway |
| Linux `revoke CLIENT_ID` | Removes that client's administrative grant; existing or renewed tokens cannot call privileged tools. Authenticated configured-root-confined listing remains available; in b3 this also removes the approved client's broader listing permission |
| Linux `stop` | Stops both identified gateway and executor; preserves OAuth state and grants for a later manual start |
| Linux `disable` | Clears all administrative grants, sets a durable disabled flag and stops both identified processes; blocks remote listing and new privileged requests while stopped and prevents an ordinary new start until explicitly enabled |
| Linux `enable` | Clears the disabled state only through the protected local operator; it does not itself authorize a client or claim to revoke old credentials |
| Pairing-code revocation | Invalidates owner pairing codes; it does not revoke previously issued OAuth tokens |

The full-disable procedure remains separate from OAuth credential-family revocation. After re-enabling and starting, retained OAuth credentials may still authenticate for listing; Linux administrative grants cleared by `disable` require new explicit approval. For long-term decommissioning, first disable the application and its dedicated ingress, reconcile outstanding work, and deliberately dispose of the protected application state using the installation's removal guide.

Stopping the gateway/executor or removing grants does not automatically undo filesystem changes or guarantee that all previously accepted jobs stop. Cancel and verify known jobs before planned shutdown. During an incident, first disable new access, then inspect the retained jobs and their exact systemd units locally. Keep logs needed for diagnosis without copying credential values into reports.

## Linux command and job limits

The Linux beta launches commands and background jobs in transient systemd services. A functioning system systemd manager, `systemd-run`, `systemctl` and `/proc` are runtime prerequisites even with manual gateway/executor startup. Unit/cgroup ownership is used to contain descendants, including children that create another session, and to verify lifecycle completion. The exact timeout, cancellation and interrupted-state guarantees must be supported by the candidate's native evidence.

Each stdout/stderr response reads at most the first 16 KiB of captured bytes, decoded as text. Replacement characters and JSON escaping can increase encoded wire size. Captured stdout/stderr storage is bounded to 1 MiB per stream, with excess drained and discarded. Read the result's truncation/limit fields; interrupted capture can leave byte-observation metadata unavailable. These limits do not impose a general CPU, memory, filesystem, network or aggregate retained-job quota. A command can write its own files or consume other root-accessible resources. Recorded limits and bounded test workloads do not qualify arbitrary adversarial workloads or unlimited retention.

Linux transfers use 16 KiB chunks with sequential offsets, exact size/hash validation and create-only publication. An existing destination is not an overwrite target. The declared maximum accepted size is not proof that that size, every filesystem or concurrent transfers were qualified. Publish only the tested sizes and behaviors listed in the acceptance record.

## Beta limits and vulnerability reports

### Optional coordination development feature

The `0.2.0a1` source adds an opt-in coordination companion and optional tools in
the existing gateways. The companion exposes no machine executor. When enabled
inside a machine gateway, approved participants still have that gateway's
administrative authority. Agent route grants, task scope records and lease tokens
are coordination controls, not a sandbox against a malicious approved host
administrator. Use distinct OAuth clients per logical agent; a shared client is
one identity even when used by multiple chats.

Enrollment, delegation routes and native destination verification are local
operator operations. A valid callback challenge proves endpoint control. The
exact native session is a separate operator-observed binding, recorded with an
evidence reference; the coordinator cannot independently obtain a provider's
private conversation identity. Task notices remain blocked until that binding is
verified and fresh. Changed callback URLs or expected sessions, replaced agents
and expired bindings fence prior claims. Routine signing-key rotation at the same
destination requires a fresh challenge and preserves a valid binding; deliveries
include both signatures for at most 300 seconds. Callback HTTP success does not
complete a task.

Coordination uses a separate protected SQLite database, with atomic task/outbox
event writes, request hashes, exclusive claims and fencing generations. Expired
running work becomes uncertain; it is not automatically requeued. A sender must
check prior effects before explicitly retrying. Tokens or fences protect task
state updates; they cannot undo a command, payment, message or remote request.
Normal completion remains worker-reported until the sender records its checks.

The database, WAL/SHM files and active worker journals contain sensitive task data,
lease material and callback signing keys. They use filesystem access protection,
not encryption at rest or operator-blind encryption. Protect the parent directory
and backups with a private POSIX mode or Windows ACL, and keep state outside the
source checkout and released artifacts. Retained tasks, events, receipts and
deduplication records have no automatic retention deletion; operators must plan
storage and backups. Per-payload/queue limits do not establish a global disk quota.

Native callbacks use public HTTPS only, validate every resolved address, pin the
connection to the checked address, verify TLS for the original hostname, reject
redirects, and bound response size and elapsed time. The live administrative grant
and registered client's revocation state are rechecked before delivery. Revoking
one access token alone is not credential-family or subscription revocation; remove
the administrative grant, disable the agent or unsubscribe to stop future native
delivery. An already in-flight callback cannot be recalled.

The supervised worker accepts a fixed operator-selected executable/argv, bounds
runtime/output and keeps coordinator credentials and leases out of the child
input/environment. It runs with its local account's permissions. Its POSIX process
group cleanup and Windows direct-child cleanup do not substitute for the released
machine executor's native containment. Model/API credentials are provisioned
separately. Live provider wake, account pairing, native Windows worker behavior and
long-running operation remain separate acceptance gates for this development feature.

Manual startup, explicit client approval and owner-managed networking are the technical-beta operating model. Persistent service/autostart/reboot behavior, broad host/client compatibility, sustained load and long-duration reliability require separate qualification. Linux acceptance does not follow from a Linux-hosted shared test suite or the Windows release. Native macOS is not qualified here.

The beta does not claim an independent security audit, a signed binary installer, a filesystem sandbox, general resource isolation or freedom from all race conditions. Focused native lifecycle and state-write tests establish only their documented cases.

Report vulnerabilities privately to the maintainer using GitHub private vulnerability reporting when enabled, or another established private channel. Include affected version, operating system, reproduction steps and redacted logs. Do not post live access/refresh tokens, token digests, owner pairing codes, cookies, credential-bearing databases, private data or tunnel credentials in public issues.
