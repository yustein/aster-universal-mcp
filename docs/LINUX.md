# Linux technical beta: 0.1.0b2

**Acceptance is artifact-specific.** This guide describes the Linux `0.1.0b2` manual deployment. These instructions and a successful package build alone do not establish release acceptance. Use the separate Linux prerelease only when its acceptance record identifies the exact archive and marks the required gates passed. [Release status](RELEASE_STATUS.md) keeps portable tests, native Linux tests, public HTTPS tests and actual Claude-account observations separate.

The reference deployment target is **Ubuntu 24.04 LTS x86_64 with distribution Python 3.12 and a functioning system systemd manager**. Exact OS, kernel, Python, systemd and dependency versions belong in the final acceptance record. Other distros, architectures, Python versions, WSL/container environments and macOS are not qualified by this candidate.

Aster runs a root OAuth gateway and a separate root Unix-socket executor. Owner-approved commands and jobs run as root. Use a disposable VM first and retain SSH, QEMU Guest Agent or another independent local administrator channel. [Read the security boundaries](../SECURITY.md) before exposing the endpoint.

## Prerequisites and deployment mode

- Ubuntu's `python3`, `python3-venv`, `systemd`, `systemctl`, `systemd-run`, `/proc`, `tar`, SHA-256 tools and local sudo/root access.
- A functioning **system** systemd manager. The command/job implementation depends on transient services even though the long-running gateway and executor are started manually.
- The reference target's unified cgroup v2 hierarchy, including `/sys/fs/cgroup/cgroup.controllers` and per-unit `cgroup.events`.
- A public HTTPS hostname under your control, with a trusted proxy/tunnel terminating locally at the Linux gateway's IPv4 loopback address.
- A Claude account that currently permits a remote custom connector and has capacity for this installation. Check the [official connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) and the account UI; do not remove an existing working connection without deciding how to preserve it.
- Correct UTC time before pairing, reliable guest control and a way to export sanitized logs independently of MCP.

If needed on the reference Ubuntu VM:

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv curl
/usr/bin/python3 --version
systemctl show --property=Version --value
systemd-run --version
uname -a
date -u
```

This package does not install persistent gateway/executor services or advertise automatic startup after reboot. The operator starts the two processes manually in the background and writes protected logs. Job transient services are a separate mechanism. Setup does not create a tunnel, change DNS/firewall rules, approve a client or publish anything.

## Files and canonical layout

The release artifact is `aster-universal-mcp-0.1.0b2-linux.tar.gz`, with source files at its root and Linux modes preserved. Obtain the matching `SHA256SUMS.txt`, `candidate-manifest.json`, Linux acceptance JSON, test-evidence archive and release notes from the same prerelease. Confirm the acceptance record's source/manifest/archive identity before installation.

| Purpose | Reference path |
|---|---|
| Frozen source | `/opt/aster-universal-mcp/0.1.0b2-source` |
| Runtime-only venv | `/opt/aster-universal-mcp/0.1.0b2-venv` |
| Protected application state | `/var/lib/aster-universal-mcp` |
| Canonical configuration | State + `/config.json`, pinned by `/installation.json` |
| Independent operator | State + `/operator-linux.py` |
| OAuth database | State + `/oauth.sqlite` and any SQLite sidecars |
| Administrator allowlist | State + `/admin-client-allowlist.json` |
| Default command/job workspace and listing root | State + `/workspace` |
| Root executor socket | State + `/run/admin.sock` |
| Job state/output and transfer staging | State + `/jobs` and `/transfers` |
| Gateway/executor logs | State + `/logs/gateway.log` and `/logs/executor.log` |
| Operator journal and dependency inventory | State + `/operator-journal.jsonl` and `/dependency-record.json` |
| Gateway listener | `127.0.0.1:18768` for the canonical setup below |

Source, venv and state must be **separate, non-nested, canonical absolute directories** with root ownership and no group/world-writable ancestors. Setup rejects symlinked control paths and refuses an existing state directory. Source and runtime dependencies are pinned; do not install test packages or write ordinary output into them.

State directories use mode 0700 and protected control files mode 0600. The ordinary unprivileged local user cannot enter this workspace or read the protected state. This is intentional: the product executes as root. The workspace organizes output outside frozen source; it is **not** a filesystem sandbox. Explicit command paths and file-transfer destinations can reach other root-accessible locations.

## Fresh runtime-only installation

Verify downloaded bytes before executing their code. From the directory containing the matching release archive and checksum file:

```bash
sha256sum --check --ignore-missing SHA256SUMS.txt
```

Require a successful checksum result for `aster-universal-mcp-0.1.0b2-linux.tar.gz`; missing files are ignored by this command, so an unrelated checksum success is insufficient. Compare the exact archive/manifest hashes with the accompanying acceptance record. The manifest verifies local identity; it does not authenticate an untrusted download.

Use new installation paths. Do not extract a new release over an existing source tree or rerun setup against live state:

```bash
(
set -eu
sudo test ! -e /opt/aster-universal-mcp/0.1.0b2-source
sudo test ! -e /opt/aster-universal-mcp/0.1.0b2-venv
sudo test ! -e /var/lib/aster-universal-mcp
sudo install -d -o root -g root -m 0755 /opt/aster-universal-mcp
sudo install -d -o root -g root -m 0755 /opt/aster-universal-mcp/0.1.0b2-source
sudo tar --extract --gzip --preserve-permissions --no-same-owner \
  --file aster-universal-mcp-0.1.0b2-linux.tar.gz \
  --directory /opt/aster-universal-mcp/0.1.0b2-source
)
```

Stop if any absence check fails. Those paths may belong to an existing installation. Preserve and inspect them before choosing a fresh versioned source/venv and isolated state path.

Run setup with your actual HTTPS origin, without a trailing slash or `/mcp` path:

```bash
sudo /usr/bin/python3 /opt/aster-universal-mcp/0.1.0b2-source/tools/setup_linux.py \
  --source /opt/aster-universal-mcp/0.1.0b2-source \
  --venv /opt/aster-universal-mcp/0.1.0b2-venv \
  --state /var/lib/aster-universal-mcp \
  --origin https://mcp.yourdomain.example \
  --port 18768
```

Setup verifies the source manifest and protected paths, checks the systemd manager, creates an empty private state and new venv, installs the exact `requirements-linux-lock.txt`, checks installed dependencies, and records the dependency/source/venv identities. It installs the independent operator outside source and returns `installed_not_started`. Inspect its JSON result and protected installation log if it fails. Failed setup retains its new paths for diagnosis; do not blindly overwrite them on retry.

For an offline installation, add `--wheel-dir /absolute/root-protected/wheel-directory`. Setup then installs only from that directory. `--existing-venv` is an explicit advanced mode for inspecting and pinning an already prepared runtime-only venv; it does not establish the fresh-installation gate. Unexpected packages beyond the runtime lock and Python bootstrap packages are rejected.

Keep tests in a separate development venv using `requirements-linux-test-lock.txt`. The first clean-install acceptance must finish before test dependencies can conceal missing runtime imports. Do not install or update packages in the pinned live venv. A changed Python executable, dependency file, link target or source inventory can correctly prevent a new start; perform maintenance as a separately verified deployment.

## Start and inspect both processes

Always invoke the installed protected operator using distribution Python, including recovery. The source copy and application venv are not the recovery entry point:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp start
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp status
```

Require both process identities and endpoints to be ready, with source and venv verification successful. Status ties the candidate, protected config, DB, allowlist, workspace, socket and port to the actual processes. Process records include boot ID, kernel start ticks, executable, argv, UID/GID, cwd and the configuration environment. The base Python executable alone is not proof of the venv or imported source. The operator also checks endpoint ownership through the processes' file descriptors.

The executor socket is root-owned mode 0600 and validates Linux `SO_PEERCRED`; ordinary local users must not be able to invoke it. Do not relax its permissions to make a client connect. The gateway itself is a root peer.

If start times out or reports a partial launch, inspect `status`, the operator journal and both process logs before another mutation. A timeout can leave a running original process. The operator reconciles a pre-journaled launch identity and refuses to steal an unknown socket or port. Reconcile and stop the identified partial deployment before starting again; do not kill whatever happens to own a recorded PID.

## Configure and verify public HTTPS

Forward your dedicated HTTPS origin to `http://127.0.0.1:18768` **on the Linux guest**. The application requires the actual peer address `127.0.0.1`, the configured public `Host`, and `X-Forwarded-Proto: https`. Uvicorn proxy-header interpretation stays disabled. A host-side proxy connecting directly to the guest's LAN address does not satisfy the loopback boundary; use an ingress fixture that terminates locally in the guest.

The connector URL is:

```text
https://mcp.yourdomain.example/mcp
```

Keep Linux's route, listener and application state separate from any Windows installation. Preserve the trusted origin checks rather than weakening them to accommodate a faulty ingress path. A discovery response proves reachability, not authenticated tool execution; an anonymous 401 proves an authentication boundary, not successful OAuth pairing. A root-path 404 alone is not a tunnel diagnosis.

Complete controlled public HTTPS acceptance before asking the account owner to pair: dynamic registration, real browser cookie/nonce behavior, wrong-code retry, owner approval, PKCE, exact-client denial/approval and native operations. Preserve failed transaction evidence before retrying an actual account flow.

## Pair the actual Claude account and approve its exact client

Check current connector capacity before changing a working Windows connection. Add a remote custom connector using the HTTPS `/mcp` URL, choose the sign-in option and automatic OAuth client registration where offered. UI labels may change; the protocol requirements do not.

When Aster's authorization page is open, issue one owner code in a separate trusted local terminal:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp pair
```

The code has 12 characters, displayed in groups of four, and defaults to a 900-second lifetime. `pair --ttl 300` can shorten its lifetime; the supported range is 30–900 seconds. It is printed only to the local caller, not the operator journal. Enter it only at your own Aster HTTPS origin and finish the redirect to Claude. Do not save it in public logs or issue replacements casually: issuing another code invalidates the earlier one, and owner-code lifetime is distinct from the pending browser request and nonce lifetimes.

OAuth pairing does not grant privileged execution. Inspect registered clients:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp clients
```

Verify the exact client ID, name and redirect URI belong to this Claude connection, then approve that ID:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp approve CLIENT_ID
```

New pairing and approval require the verified candidate and both canonical running processes. Never approve an unexpected client or expose local operator actions as remote MCP tools. If pairing fails, capture the single failed transaction's sanitized metadata and diagnose it before asking for another attempt.

## Tools, default working directory and limits

Derive calls from the Linux server's actual tool schema. Linux names and result shapes differ from Windows.

| Linux tool | Behavior |
|---|---|
| `list_directory` | Authenticated listing confined to configured root; default `path="."`; does not consult the administrator allowlist |
| `run_command` | Exact argv list in the `command` argument, structured exit/stdout/stderr and bounded timeout; omitted `cwd` uses the workspace |
| `start_job` | Background command in a transient systemd service; omitted `cwd` uses the same workspace |
| `job_status`, `cancel_job` | Inspect/cancel the exact job and reconcile its recorded unit/process identity |
| `begin_upload`, `upload_chunk`, `finish_upload` | New absolute destination, sequential chunks and size/SHA-256 verification; refuse overwrite |
| `download_info`, `download_chunk` | File size/hash plus bounded base64 chunks |

All tools except listing require the client's administrative grant on each invocation, including calls with renewed OAuth tokens. To observe identity through MCP, use `run_command` with `{"command":["hostname"]}` and `{"command":["id"]}`. Check the returned status, exit code, stdout and stderr; HTTP 200 by itself does not establish success.

Commands and jobs run in transient systemd units so cancellation and timeout can cover their cgroups, including children that call `setsid`. Native acceptance must demonstrate actual worker/child exit and ceased file effects before claiming that behavior. Do not infer completion from a status label. A functioning system manager remains required in manual gateway mode.

Each response stream reads at most the first 16 KiB of captured bytes and decodes them as text; replacement characters and JSON escaping can increase the encoded response size. Capture retains at most 1 MiB per stream while excess output is drained and discarded. These bounds are **not general CPU, memory, filesystem, network or aggregate retained-job quotas**. Commands can write their own files and consume other root-accessible resources. Retained jobs/logs still need operator review. Claim only the timeout, cancellation, output and race cases in the matching acceptance record.

An executed process returns `status="completed"` with its actual exit code, including nonzero exits; a missing executable returns `failed`. Timeout is terminal only after unit/cgroup quiescence is verified. Pending or uncertain lifecycle operations retain `lifecycle_pending`/`recovery_required`; even `run_command` returns a job ID for reconciliation. Interrupted capture can leave byte-observation/truncation metadata unavailable; retained spool size is bounded independently.

Transfers use chunks up to 16 KiB. Acceptance must cover a small file spanning several chunks, exact size/hash recovery, wrong offset/hash, incomplete finish, protected staging and refusal to replace a sentinel. The declared 2 GiB size ceiling is not a tested-size claim. Download hash recovery assumes the source file remains unchanged between calls. The protected default workspace is the tested destination; adversarial changes to destination parent directories and arbitrary filesystems are not qualified. Do not assume concurrent transfers or very large payloads were qualified.

## Stop, restart and permissions

For a planned stop, finish or cancel known jobs through MCP and verify actual termination first. The gateway/executor are separate from job systemd units; stopping both processes does not automatically cancel previously launched jobs.

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp stop
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp status
```

`stop` preserves the OAuth database and grants. A subsequent `start` verifies the same pinned deployment and starts both processes. The same client credentials and approval should remain usable; the matching acceptance record must demonstrate it. No reboot/autostart behavior is claimed.

Remove one client's administrative permission:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp revoke CLIENT_ID
```

This blocks subsequent privileged tools for that client, including with existing or renewed access tokens. It does **not** revoke its OAuth credentials, cancel previously accepted jobs, undo completed changes or remove authenticated directory-listing access. Listing remains confined to the configured root.

To disable **all remote access** through this deployment:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp disable
```

`disable` clears all administrative grants, writes a durable disabled flag and stops both identified processes. Confirm the stop result and absent listener. The flag prevents a normal new start until explicitly enabled. If shutdown identity is uncertain, isolate the dedicated public route and resolve the local process identity; do not assume the flag alone stopped an already running gateway.

To return to service after resolving the reason for disabling:

```bash
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp enable
sudo /usr/bin/python3 /var/lib/aster-universal-mcp/operator-linux.py \
  --state /var/lib/aster-universal-mcp start
```

`enable` verifies the pinned deployment, clears the disabled flag, and restores neither grants nor processes. Reapprove intended clients explicitly after inspecting `clients`. Retained OAuth credentials can still authenticate under their original rules. Grant removal, full disable and pairing-code revocation are not OAuth credential-family revocation; the public revocation endpoint is not enabled.

## Recovery when source or venv is unavailable

The protected operator uses `/usr/bin/python3` and standard-library code outside the application source/venv. `status`, `revoke`, `stop` and `disable` remain the recovery entry points if those application paths become unavailable. Status reports the source/venv verification failure while still inspecting recorded runtime identity. Starts, new approvals, pairing and enable fail closed on unexpected source/configuration state.

Retain local root access, distribution Python, the protected operator/config/installation records and a pristine private rollback copy. Recovery does not claim to work if those independent trust records or the operating system itself are destroyed. Do not rewrite the manifest or its recorded hash to force acceptance of unexplained changes.

Before recovery or maintenance, journal the intended mutation and keep the sole pristine backup untouched. Stop and verify identified processes before a consistent private state backup, including any SQLite sidecars. Keep code, venv, state and evidence copies distinct. If an operation times out, inspect its original journal/runtime/process result before repeating it.

The operator signals an identified process through a retained Linux pidfd after rechecking boot/start identity, executable and configuration bindings. If it reports a reused or ambiguous identity, preserve evidence and inspect locally. Never use an old handoff PID as a fresh kill target.

Known jobs may remain in separate systemd units after gateway recovery. Inspect only the recorded Aster-owned job IDs/unit names; cancel and verify those units individually. Do not issue broad process-name kills or stop unrelated services. Remove only acceptance-owned markers/transfers/jobs during test cleanup.

## Removal and evidence

Disable the application, verify both identified processes have stopped, disable its dedicated ingress, and reconcile any outstanding job units before decommissioning. Preserve a private recovery copy if needed. Only then remove the exact source, venv and state paths belonging to this installation. The guide deliberately provides no blanket recursive removal command that could erase an unrelated deployment. Removing the state permanently discards registrations, grants and local evidence; it is different from ordinary stop/restart.

Public acceptance evidence must identify the frozen source commit/tree, manifest/archive hashes, actual environment, dependency record, harness, each test layer, real account observations and final cleanup. Keep failures and their later reconciliation. Never include live codes, tokens, cookies, token digests, credential-bearing SQLite files or recovery credentials in release assets.

The software remains Apache-2.0 with voluntary donations and no required paid license or activation. Publication of this Linux candidate awaits the separate documented gates; the working Windows `v0.1.0b1` release remains unchanged.
