# Amsterdam Coordinator operator deployment

These assets deploy the existing PR #6 companion independently of Aster PA on
the current Amsterdam host. They do not change PA listeners, databases, accounts,
the archive, DNS or Cloudflare configuration. Core source baseline:
`69b60816b4d316cb0f6ddc96bcf6727b9c38aa8a` (`0.2.0a1`).

The installer prepares files and reloads systemd. **It does not enable, start,
stop or restart a service.** Remote changes remain the operator's explicit
deployment action. No privileged machine executor is enabled in the companion.

## Layout and service limits

| Path | Purpose |
| --- | --- |
| `/opt/aster-coordinator/releases/<wheel-sha>/venv` | Root-owned versioned runtime |
| `/opt/aster-coordinator/venv` | Managed runtime symlink |
| `/opt/aster-coordinator/operator.py` | Local stdlib config and recovery helper |
| `/etc/aster-coordinator/service.env` | Preserved issuer, file paths and listener config |
| `/var/lib/aster-coordinator` | Private state, user/group `aster-coordinator`, mode 0700 |
| `tasks.sqlite` | Task state, native subscriptions, callback keys and outbox |
| `oauth.sqlite` | OAuth registrations, approvals and rotating credentials |
| `approved-clients.json` | Exact approved client IDs, mode 0600 |
| `operator.lock` | Shared application/operator and exclusive backup lock |
| `/var/backups/aster-coordinator` | Root-private local recovery sets |
| `/var/lib/aster-coordinator-restore-check` | Root-private temporary offline restores |

The service binds `127.0.0.1:18769`, runs as `aster-coordinator`, uses a private
umask, and can write only its state directory. The unit caps memory at 512 MiB
(384 MiB high watermark), disables its swap use, limits CPU to 50% of one CPU,
and limits tasks to 64. Native callback delivery retains outbound DNS/HTTPS.

## Install reviewed artifacts

Prerequisites are a root administrator channel, Python >=3.11 with `venv`,
systemd, util-linux (`flock`, `runuser`), and an independently reviewed wheel.
The copied lock file pins the same Linux dependency versions as the source.
Installation resolves those dependencies through pip; no OS packages are
installed automatically. The wheel's exact supplied SHA-256 is checked first.

From the directory containing these assets:

```sh
sudo bash install.sh /absolute/path/aster_universal_mcp-0.2.0a1-py3-none-any.whl \
  ACTUAL_REVIEWED_WHEEL_SHA256 https://coord.27.fyi
```

An existing config is parsed and preserved byte-for-byte, including a changed
valid port. A conflicting origin stops installation rather than rewriting the
configuration. Existing task/OAuth databases and allowlist contents are never
replaced. An already complete hash-named release is reused. An incomplete release
directory is left for inspection; the installer does not erase it to retry.

The installer requires the Coordinator to be stopped and checks that its state
directory belongs to the dedicated account with mode 0700. It prepares a fresh
empty allowlist only when none exists. It creates no OAuth grants, agents, routes
or native subscriptions. Keep source snapshots and recovery copies outside this
live state directory.

## Permanent HTTPS ingress

Configure the dedicated `coord.27.fyi` ingress to the loopback listener. Source
`PublicOriginGate` requires all three properties:

- Actual peer at the application is `127.0.0.1`.
- `Host` is exactly `coord.27.fyi`.
- `X-Forwarded-Proto` is exactly `https`.

A local Caddy reverse-proxy stanza may use:

```caddyfile
coord.27.fyi {
    reverse_proxy 127.0.0.1:18769 {
        header_up Host coord.27.fyi
        header_up X-Forwarded-Proto https
    }
}
```

For a dedicated named Cloudflare tunnel, use `http://127.0.0.1:18769` as origin,
set the original HTTP host to `coord.27.fyi`, and verify the actual forwarded
scheme. If a local proxy is needed, apply the same headers there. Do not attach a
different host as a replica of an existing tunnel whose routes address another
machine's localhost. Preserve every existing PA/dashboard route.

After inspecting the new ingress and unit, activate explicitly:

```sh
sudo systemctl enable --now aster-coordinator.service
sudo systemctl show aster-coordinator.service -p ActiveState -p MainPID -p MemoryCurrent
```

Check the public OAuth discovery and MCP behavior, then use the actual connected
client's authenticated `connection_status`. An unauthenticated HTTP response or
a loopback listener alone does not prove account access. Confirm the tool reports
`service=coordination_companion`, the intended client, administrative approval,
and `machine_execution=false`.

## Operator ownership and locking

The service holds a **shared** `operator.lock` for its entire lifetime. Run every
local enrollment, approval, pairing and allowlist action under the same shared
lock. The backup helper takes the exclusive lock and verifies the systemd unit
is inactive with MainPID 0. Do not run legacy direct CLI writers outside this
procedure during a backup; an advisory lock cannot fence an unrelated root
process that ignores it.

Run commands that do not require root as the service user. For example:

```sh
sudo runuser -u aster-coordinator -- flock --shared \
  /var/lib/aster-coordinator/operator.lock \
  /opt/aster-coordinator/venv/bin/python -m admin_client_allowlist \
  --db /var/lib/aster-coordinator/oauth.sqlite \
  --allowlist /var/lib/aster-coordinator/approved-clients.json clients
```

After verifying the exact registration, use the same prefix with the `approve
EXACT_CLIENT_ID` action. The allowlist CLI atomically replaces its file with mode
0600. Running it as root would make the replacement unreadable by the service;
using the service account preserves the correct ownership.

The legacy `owner_approval_cli` requires root. If it is necessary, hold the same
shared lock and keep credentials out of shell output/history. Afterwards check
and restore the dedicated UID/GID on the exact OAuth DB and any existing
`oauth.sqlite-wal`, `oauth.sqlite-shm`, or `oauth.sqlite-journal` sidecars before
releasing the operator lock. Never apply recursive ownership changes to other
services. Normal browser pairing through the running companion writes as the
service user.

Agent enrollment follows `docs/AGENT_COORDINATION.md`: already approved exact
OAuth registrations, distinct logical identities, explicit directional grants,
then host-created event subscriptions. Verify the actual probe in each intended
conversation before calling local `verify-receiver`. The main sender also needs
a verified receiver if its project continuation is to wake automatically.

Moving from a temporary origin to `coord.27.fyi` requires legitimate
reauthorization for the new OAuth resource. Do not rewrite stored token audiences
or copy another conversation's callback to manufacture receiving readiness.

## Consistent application backup

Each recovery set is a private directory with exactly:

```text
tasks.sqlite
oauth.sqlite
approved-clients.json
service.env
manifest.json
```

The manifest contains member hashes/sizes, time, origin, schema/count checks and
stopped-service evidence. It contains no raw task output or credential values.
The files themselves contain sensitive state; the local directory's mode 0700
and files' mode 0600 provide access control, **not encryption**. Use the existing
approved encrypted offsite mechanism for an independent recovery copy. This
package does not configure an offsite destination or backup schedule.

Record whether the Coordinator was active. Stop only this service, quiesce local
writers, and choose a new backup directory. Leave PA and the archive running.

```sh
sudo systemctl stop aster-coordinator.service
sudo python3 /opt/aster-coordinator/operator.py backup \
  --config /etc/aster-coordinator/service.env \
  --output /var/backups/aster-coordinator/UNIQUE_SNAPSHOT_NAME
```

The helper uses SQLite's backup API, which includes committed WAL contents. It
does not copy only the main SQLite files. It verifies both databases and the
application schema, preserves allowlist/config bytes, and publishes the snapshot
directory only after complete verification. On failure it removes its partial
staging directory, leaves existing backups unchanged, and never changes service
state. Restart the service if it was active before this operation; preserve an
intentionally stopped service's state. If restarting fails, report it rather than
calling the backup cycle healthy.

## Actual isolated restore verification

```sh
sudo python3 /opt/aster-coordinator/operator.py restore-verify \
  --backup /var/backups/aster-coordinator/UNIQUE_SNAPSHOT_NAME \
  --work-parent /var/lib/aster-coordinator-restore-check
```

This creates a new private temporary directory, copies every fixed recovery
member, verifies file bytes, SQLite integrity, schema and application counts, then
removes the temporary state. It does not start the copied companion or native
dispatcher. It returns counts and pass/fail only, so callback URLs, signing keys,
OAuth material, task text and lease tokens are not printed.

Restore verification cannot overwrite a live state directory. Promoting a backup
to replace live state is a separate controlled operation: fence the old writer,
reconcile newer accepted work and revocations, preserve the intended origin, and
reauthorize/reverify destinations when required. Do not extend expired leases or
requeue uncertain work merely because an older copy was restored.

## Qualification for this deployment

For restart evidence, record the accepted task's task/outcome IDs and result
hashes through the real sender connection, restart only the Coordinator service,
then read the same results through that connection. A systemd active state alone
does not satisfy preservation or receiving readiness. Verify the actual intended
worker bindings after restart, and keep saved result, sender acceptance, visible
publication and parent continuation as separate observations.

Focused local asset tests:

```sh
python -m unittest discover -s tests -p test_coordinator_deployment.py -v
bash -n ops/coordinator/install.sh
```

Tests use disposable state and mocked service inspection. They cover committed
WAL capture, retained accepted work, private outputs, active-service/lock refusal,
partial-backup cleanup, tampering/symlinks, no overwrite, offline restore cleanup,
invalid config, origin preservation and idempotent config preparation. They do
not establish an actual host restart, native callback, account connection or
public TLS result.
