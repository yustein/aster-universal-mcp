# Security boundaries — privileged administration beta

Aster deliberately gives an approved remote MCP client powerful host-execution and file-access tools. Install it only on a machine you own and control, begin on a disposable machine, and retain an independent local administrator session. Windows `0.1.0b1` is the published technical beta. Linux `0.1.0b2` is a separate candidate whose required evidence is tracked in [release status](docs/RELEASE_STATUS.md); this security description is not acceptance or an independent audit.

Windows commands inherit the gateway account's permissions; the accepted reference VM used SYSTEM. The Linux candidate uses a root OAuth gateway and a separate root Unix-socket executor, and its approved commands/jobs run as root. Neither model is a sandbox against an approved administrator or the local root/Administrator account.

## Network and authorization

| Boundary | Windows | Linux candidate |
|---|---|---|
| Gateway | IPv4 loopback, default `127.0.0.1:18767` | IPv4 loopback, canonical setup default `127.0.0.1:18768` |
| Public ingress | Owner-controlled HTTPS proxy/tunnel | Separate owner-controlled HTTPS proxy/tunnel |
| Privileged execution | Gateway account | Root executor over a protected Unix socket |
| Local executor access | Platform-specific Windows implementation | Root-owned mode 0600 socket plus Linux `SO_PEERCRED` root-peer check |
| Remote privileged tools | OAuth `system:admin` scope and exact local client grant | OAuth `system:admin` scope and exact local client grant |
| Directory listing | See versioned Windows guide | OAuth authenticated, confined to configured listing root; independent of the administrator allowlist |

The trusted ingress must connect from `127.0.0.1`, preserve the configured public `Host`, and supply `X-Forwarded-Proto: https`. Do not expose the raw gateway listener on a public interface. If the proxy is on another machine, use a locally terminating channel that preserves this boundary; a direct guest-LAN connection is not equivalent to loopback. Setup does not provision ingress or change firewall rules.

Owner browser pairing establishes OAuth authorization. Privileged tools require a separate exact-client administrative grant made locally after verifying that client's identity and registered redirect URI. There is no shared default administrator password or wildcard grant. Protect the OAuth database, its sidecars and browser approval material as credentials; keep tunnel credentials outside source and release archives.

Access tokens are short-lived and refresh tokens rotate. The shared resource-binding check rejects a wrong, empty or duplicated explicit token-request resource before consuming an otherwise valid code or refresh grant. Omitted resource remains supported for the deployment's bound audience. The public OAuth revocation endpoint is not enabled, and this beta does not claim access/refresh credential-family tracking or family revocation.

Commands, outputs and file contents requested through MCP can reach the connected AI service. Use data appropriate for that service and review requested operations. OAuth and administrator approval establish permission; they do not make an arbitrary root command safe.

## Source, workspace and protected state

Verify the archive SHA-256 using the trusted release checksum before installing. The source manifest establishes local byte identity; it does not authenticate an unknown download or protect against root deliberately replacing both source and trust records. Do not regenerate the manifest to dismiss unexplained source drift.

Windows setup restricts `%ProgramData%\AsterUniversalMCP` to SYSTEM and Administrators. It contains configuration, OAuth state, allowlist, runtime records, logs and the default workspace. Use the [versioned Windows instructions](docs/WINDOWS.md) for its protected operator and ACL model.

The Linux layout separates root-owned frozen source and venv under `/opt/aster-universal-mcp` from protected operational state under `/var/lib/aster-universal-mcp`. State contains the canonical config, OAuth database, allowlist, independent operator, runtime identities, logs, job/transfer state and workspace. State/control directories and files are protected from ordinary unprivileged local users. Work output belongs in the workspace or another deliberate path outside frozen source. An ordinary local user is not given access to protected state merely to make workspace use convenient.

On Linux, commands and jobs that omit `cwd` use the configured workspace. Listing uses its configured root. Explicit command paths and transfer paths can reach other locations available to root; neither the workspace nor listing confinement restricts an approved root command's filesystem access.

New starts, owner-code issuance and new administrative approvals require the verified source/configuration state. Each platform installs an independent protected operator so access removal and safe identified shutdown remain available when application code or its venv is missing or damaged. The Linux operator uses distribution Python and its standard library rather than importing application modules. Keep that protected operator, distribution Python and the local administrator channel available. See the [Linux recovery procedure](docs/LINUX.md#recovery-when-source-or-venv-is-unavailable).

Lifecycle operations must identify the actual process, including its start identity, executable and relevant listener/socket, before acting. PID alone is insufficient. If an identity cannot be established, the operator must refuse to terminate an unrelated process; isolate the dedicated public route and inspect local evidence before manual intervention.

## Access removal and full disable

Removing a connector in Claude does not clear persisted host grants or OAuth credentials. Removing a host grant does not undo completed actions or revoke a credential family.

| Operator action | Effect and limit |
|---|---|
| Windows `revoke` | Removes one client's administrative grant |
| Windows `disable` | Removes all administrative grants; `-StopGateway` also stops the verified gateway |
| Linux `revoke CLIENT_ID` | Removes that client's administrative grant; existing or renewed tokens still cannot call privileged tools, but authenticated root-confined listing remains available |
| Linux `stop` | Stops both identified gateway and executor; preserves OAuth state and grants for a later manual start |
| Linux `disable` | Clears all administrative grants, sets a durable disabled flag and stops both identified processes; blocks remote listing and new privileged requests while stopped and prevents an ordinary new start until explicitly enabled |
| Linux `enable` | Clears the disabled state only through the protected local operator; it does not itself authorize a client or claim to revoke old credentials |
| Pairing-code revocation | Invalidates owner pairing codes; it does not revoke previously issued OAuth tokens |

The full-disable procedure remains separate from OAuth credential-family revocation. After re-enabling and starting, retained OAuth credentials may still authenticate for listing; Linux administrative grants cleared by `disable` require new explicit approval. For long-term decommissioning, first disable the application and its dedicated ingress, reconcile outstanding work, and deliberately dispose of the protected application state using the installation's removal guide.

Stopping the gateway/executor or removing grants does not automatically undo filesystem changes or guarantee that all previously accepted jobs stop. Cancel and verify known jobs before planned shutdown. During an incident, first disable new access, then inspect the retained jobs and their exact systemd units locally. Keep logs needed for diagnosis without copying credential values into reports.

## Linux command and job limits

The Linux candidate launches commands and background jobs in transient systemd services. A functioning system systemd manager, `systemd-run`, `systemctl` and `/proc` are runtime prerequisites even with manual gateway/executor startup. Unit/cgroup ownership is used to contain descendants, including children that create another session, and to verify lifecycle completion. The exact timeout, cancellation and interrupted-state guarantees must be supported by the candidate's native evidence.

Each stdout/stderr response reads at most the first 16 KiB of captured bytes, decoded as text. Replacement characters and JSON escaping can increase encoded wire size. Captured stdout/stderr storage is bounded to 1 MiB per stream, with excess drained and discarded. Read the result's truncation/limit fields; interrupted capture can leave byte-observation metadata unavailable. These limits do not impose a general CPU, memory, filesystem, network or aggregate retained-job quota. A command can write its own files or consume other root-accessible resources. Recorded limits and bounded test workloads do not qualify arbitrary adversarial workloads or unlimited retention.

Linux transfers use 16 KiB chunks with sequential offsets, exact size/hash validation and create-only publication. An existing destination is not an overwrite target. The declared maximum accepted size is not proof that that size, every filesystem or concurrent transfers were qualified. Publish only the tested sizes and behaviors listed in the acceptance record.

## Beta limits and vulnerability reports

Manual startup, explicit client approval and owner-managed networking are the technical-beta operating model. Persistent service/autostart/reboot behavior, broad host/client compatibility, sustained load and long-duration reliability require separate qualification. Linux acceptance does not follow from a Linux-hosted shared test suite or the Windows release. Native macOS is not qualified here.

The beta does not claim an independent security audit, a signed binary installer, a filesystem sandbox, general resource isolation or freedom from all race conditions. Focused native lifecycle and state-write tests establish only their documented cases.

Report vulnerabilities privately to the maintainer using GitHub private vulnerability reporting when enabled, or another established private channel. Include affected version, operating system, reproduction steps and redacted logs. Do not post live access/refresh tokens, token digests, owner pairing codes, cookies, credential-bearing databases, private data or tunnel credentials in public issues.
