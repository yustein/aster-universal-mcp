# Conversation archive deployment

This package starts a separate PostgreSQL 18.6 archive, import watcher, durable
job worker, and authenticated read-only MCP service. It does not modify Aster PA,
the Coordinator's database, existing host-execution MCP services, DNS, or tunnels.
The Docker deployment has not yet been qualified on Lenomila. The application
can be tested against a local PostgreSQL instance independently of this package.
The image pins Python **3.12.14** and installs the checked-in
`requirements-archive-lock.txt` before installing the application without
dependency resolution. The Dockerfile-specific ignore file excludes private
configuration and export data from the build context.

## Host qualification

The initial deployment targets Lenomila's native Ubuntu host with Docker Engine
and Compose v2. Keep PostgreSQL and archive volumes on its Linux filesystem.
Before enabling the services, verify the actual host identity, startup and shutdown behavior,
available RAM and disk, sustained disk writes, existing workload headroom, and
the chosen bridge subnet. Do not infer Lenomila's capacity from Amsterdam's PA
host. Qualify the host's sleep and reboot settings for continuous operation.

The starting container caps are 1 GiB/1 CPU for PostgreSQL, 1 GiB/1 CPU for the
worker, 256 MiB/0.25 CPU for the watcher, and 512 MiB/0.5 CPU for MCP. Migration
and backup jobs each have a temporary 512 MiB limit. These are initial operating
limits, not measured throughput or a hardware recommendation. Leave additional
host/VM headroom and measure the largest conversation before increasing workers.
The worker processes bounded jobs; a per-source database lock protects imports
of the same account/workspace from concurrent reconciliation.

`ASTER_ARCHIVE_MIN_FREE_BYTES` reserves **10 GiB** of free-space headroom in this
deployment; the application default outside Compose is 1 GiB. Before copying an
export, available space must cover that reserve **plus the complete source file
size**. Before each conversation transaction, the importer checks the reserve
again. Low space produces a retryable I/O failure and preserves committed
checkpoints; it does not quarantine the export as malformed. The setting is a
positive byte count, bounded at 1 PiB. Checks measure the filesystem backing the
archive/CAS root, so a PostgreSQL volume on a different filesystem needs separate
capacity monitoring. These checks provide headroom, not a quota against other
host processes; verify free space and resolve low-space receipts before retrying.
Malformed reserve configuration fails worker, dispatch and watcher startup before
pending imports can be misclassified as invalid source data.

## Prepare private configuration

From this directory:

```bash
umask 077
# This public script is bind-mounted and read by PostgreSQL UID 999.
# A clone made under a private umask can otherwise leave it unreadable.
chmod 644 init-db.sh
cp .env.example .env
chmod 600 .env
mkdir -p private/config
chmod 700 private
chmod 755 private/config
printf '%s\n' '{"version":1,"clients":{}}' > private/config/clients.json
chmod 644 private/config/clients.json
sudo chown root:root private/config private/config/clients.json
```

Generate **three different** passwords locally with `openssl rand -hex 32` and
put them in `.env`. Hexadecimal passwords avoid URI-escaping errors in the DSNs.
Keep the file private; do not commit it, paste it into chat, or publish rendered
Compose configuration containing its values. The configuration directory and
ACL must be owned by root or container UID 10001, with no group/other write bits.
Do not use symlinks for the ACL.

Set `ASTER_ARCHIVE_INBOX` to an existing absolute host directory, such as
`/srv/aster-archive/inbox`. Grant container UID 10001 read/traverse access to its
finalized files; retain suitable owner access for the process that delivers the
exports. The inbox is mounted read-only in the watcher and worker. Mounts fail
if the configured source directory does not exist.

Choose stable `ASTER_ARCHIVE_OWNER` and `ASTER_ARCHIVE_SOURCE` identifiers. Use a
separate source for a different ChatGPT account/workspace. Reuse the same source
for every subsequent export from that account/workspace. These are archive
identities, not permission to access someone else's conversations.

Set a **dedicated HTTPS origin** for this archive in
`ASTER_ARCHIVE_PUBLIC_ORIGIN`. The sample `.invalid` origin is a placeholder.
The service has its own OAuth database; never mount the privileged host
executor's OAuth database here.

### Reverse proxy and loopback binding

The container listens on port 8770 internally, published only on
`127.0.0.1:8770` on the host. PostgreSQL has no host port. A separately configured
HTTPS reverse proxy or tunnel must reach this loopback endpoint and preserve
the expected public host/origin information.

With Docker port forwarding, the application's immediate peer is normally the
bridge gateway, not host `127.0.0.1`. Verify that observed peer, then set the
**exact** `ASTER_ARCHIVE_TRUSTED_PROXY_HOST`. Do not use a wildcard. The sample
subnet/gateway `172.30.77.0/24` / `172.30.77.1` must first be checked for conflicts
with the host's routes and other Docker networks. Adjust all three network and
proxy settings together if needed. Unverified proxy settings should fail closed.
This package does not establish public connectivity by itself.

## Start and check

From this directory, after configuration and host qualification:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 migrate watch worker mcp
```

The one-time PostgreSQL initialization creates a non-superuser ingest role and
a separate reader role. The reader has SELECT access to the `aster_archive`
schema and defaults to read-only transactions; it has no queue-table privileges.
The `migrate` service creates/upgrades archive and Procrastinate tables before
other application services start. Initialization scripts run only for a new
empty PostgreSQL volume. Changing passwords in `.env` does not rotate existing
database role passwords; perform an explicit coordinated rotation instead.
The initializer applies role, schema and grant changes in one transaction. The
database health check requires both archive roles and the correctly owned
archive schema, as well as a responding TCP listener; PostgreSQL's temporary
Unix-only initialization server does not satisfy this check. If initialization
fails, inspect its logs and repair the specific cause before starting imports;
an initialized data directory causes PostgreSQL to skip entrypoint scripts on
the next start. Never wipe an existing volume as an initialization retry.

The PostgreSQL 18 image uses `PGDATA=/var/lib/postgresql/18/docker`, with the
persistent volume mounted at **`/var/lib/postgresql`**. Do not substitute the
older `/var/lib/postgresql/data` mount. Do not run `docker compose down -v` on an
archive you intend to retain.

Use the CLI's status command with the same owner and source identifiers used in
`.env`:

```bash
docker compose exec worker python -m aster_archive status \
  --owner owner-personal --source chatgpt-personal
```

Inspect import states, record counts, error codes, and coverage. An idle process
or a healthy PostgreSQL socket does not prove that an export was fully imported.
This release does not include an HTTP health endpoint or an external monitor.

## Deliver the requested export and later exports

Download or transfer the complete export to a staging filename ending in
`.partial`, or into a separate staging directory. After the transfer completes,
rename it atomically into the inbox as a final `.zip`. Use a new filename for
each export rather than overwriting a file being read. An extracted supported
conversation JSON file can also be finalized with a `.json` extension.

The watcher examines finalized `.zip`/`.json` files after their size and modified
time stay unchanged across two scans. The importer independently checks source
stability and container validity. A size plateau alone is not proof of a finished
download; the `.partial` → final rename procedure remains the delivery contract.
It records immutable original bytes, queues the work, and reconciles records
using source IDs and content hashes. Re-delivering unchanged source bytes is
safe. Keep the original ZIP even if a readable HTML transcript is also present.

For a deliberate one-off import through the same mounted inbox:

```bash
docker compose exec worker python -m aster_archive import /imports/export.zip \
  --owner owner-personal --source chatgpt-personal
```

Do not rename or remove an inbox file while its import is pending. Inspect status
after errors; do not repeatedly resubmit unknown jobs without their receipts.
The durable worker and recovery handling allow interrupted work to be resumed,
but successful repeat-import and restart checks must be verified on the deployed
host before calling the service operational.

The watcher imports delivered files. It does not request exports, sign in to
ChatGPT, monitor email, or provide an account-wide live conversation feed.
During active work, obtain a supported fresh export weekly and review coverage
daily; scheduling export acquisition remains a separate account capability.

## Connect an authorized ChatGPT client

After HTTPS routing is verified, register the dedicated archive MCP endpoint
`https://YOUR-ARCHIVE-ORIGIN/mcp` using the existing owner pairing flow. This
service reuses Aster's OAuth implementation and its current `system:admin`
scope name, but exposes only the archive's read tools. It contains no command,
file-upload, raw-SQL, or Coordinator-write MCP tools.

Issue a short-lived pairing code **in a local operator terminal**, after MCP has
created its private OAuth database:

```bash
./operator.sh issue --ttl 300
```

Keep that code in the local owner approval flow. Do not paste it into chat or
store it in deployment files. Inspect the exact registered client identity and
intended origin during approval. The current local approval helper can inspect
a pending request without printing an access token:

```bash
./operator.sh inspect -- REQUEST_ID
./operator.sh approve -- REQUEST_ID
```

The approval command prompts for the one-time code in the terminal without
echoing it or placing it in shell history or OS process arguments. Run only one
local owner operation at a time. The wrapper briefly stops the archive MCP
service and restarts it on exit only if it was running; imports continue. Finish
the short-lived approval before its pending request expires.

These operations use a separate `operator` profile: real UID 0, only
`DAC_OVERRIDE`, `FOWNER`, and `CHOWN` capabilities, no network, no database
password environment, and only the private OAuth volume. The existing helpers'
root checks remain in place. The wrapper restores the auth directory to UID/GID
10001 and mode 0700, and its known SQLite database/WAL/SHM/journal files to
UID/GID 10001 and mode 0600, including when the helper reports an error. Container
logging is disabled for this one-shot profile so pairing codes remain in the
local terminal. Do not run the root helpers directly inside the `mcp` service:
that service drops all capabilities and its private auth files belong to UID
10001.

Normal signal termination runs ownership cleanup. A forced kill or host failure
cannot run `finally`; before resuming MCP after such an interruption, run
`./operator.sh revoke` locally to restore ownership and revoke any outstanding
pairing code, then inspect service status. The Unix ownership/WAL wrapper has
local tests; the complete Docker operator flow still requires host qualification.

OAuth pairing alone does not grant archive access. Add the **exact** approved
OAuth client ID to `private/config/clients.json`, keeping ownership and mode
constraints. An owner-wide entry is:

```json
{
  "version": 1,
  "clients": {
    "EXACT_APPROVED_OAUTH_CLIENT_ID": {
      "owner_id": "owner-personal",
      "source_ids": "*",
      "project_ids": "*"
    }
  }
}
```

Use explicit source/project ID lists for scoped worker access. Empty `clients`
denies everyone; a missing client or an unclassified project does not acquire
scope by inference. Do not modify the machine executor's administrator allowlist
to enable archive access. Recreate the `mcp` service after changing its ACL so
configuration reloading is explicit.

Verify from a fresh authenticated ChatGPT connection: search a known synthetic
phrase, fetch its exact passage, confirm citation and source IDs, check context,
then confirm an unauthorized source/client is denied. Canonical passage URLs
require the same authorization; a normal browser click without a bearer token
currently returns 401. No anonymous conversation transcript is published.

## Back up and qualify restoration

Run the repository's helper from an authorized local operator terminal:

```bash
../../scripts/archive-backup.sh /srv/aster-archive-backups
```

The helper briefly stops only archive `watch`/`worker` services that were running,
takes a consistent PostgreSQL custom-format dump, snapshots the live OAuth SQLite
database using SQLite's backup API, copies original archive objects and the client
ACL, records the private deployment environment, and computes SHA256 checksums.
It then restarts only the archive services it stopped. The MCP reader can remain
running. Do not run other archive import/purge/configuration commands concurrently
with this backup. Partial output remains private if a step fails; a directory
becomes completed only after all backup components succeed.

Treat the complete backup as sensitive: it contains conversations, OAuth state,
and database passwords. Configure an independent encrypted offsite target and
retention policy, for example with restic, before declaring backup coverage.
No offsite destination, retention schedule, or successful restoration is claimed
by this package. Never back up a running PostgreSQL data directory with ordinary
file copying.

Restore qualification requires a **separate isolated environment**: verify the
checksums; initialize the matching database roles; restore `database.dump` with
`pg_restore --no-owner --no-acl --role=aster_archive_ingest` into an empty archive
database; restore archive objects, auth snapshot, and client ACL with UID 10001
ownership and private modes; then check counts, exact passages, repeat import,
and job reconciliation before enabling the watcher. Restore OAuth state only to
the intended service identity; a test environment must not reuse live public
routing or accidentally accept production tokens. Do not overwrite the active
archive to test a backup.

## Present scope and remaining qualification

Phase one imports and preserves exported conversations and enables lexical
retrieval. Embedding generation, pgvector indexes, OCR/transcription, knowledge
claims, and optional Cognee enrichment are later stages with separate receipts
and resource budgets. There are no paid model calls in this deployment.

Before reporting Lenomila operational, verify: the actual Linux runtime and
resources; finalized export receipt; successful parsing and coverage; repeat
import; interrupted-worker recovery; restart after host reboot; authenticated
MCP retrieval and access denial; an isolated backup restoration; and the
independent offsite copy. These are deployment checks, not reasons to delay local
development while the requested export is being prepared.

References: [PostgreSQL image storage and initialization](https://github.com/docker-library/docs/blob/master/postgres/README.md),
[PostgreSQL logical backups](https://www.postgresql.org/docs/current/backup-dump.html),
[Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).
