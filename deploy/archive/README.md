# Conversation archive deployment

This package starts a separate PostgreSQL 18.6 archive with pgvector 0.8.7,
an import watcher, durable import and enrichment workers, and an authenticated
read-only MCP service. It does not modify Aster PA, the Coordinator's database,
existing host-execution MCP services, DNS, or tunnels.
Native bootstrap, durable synthetic import, exact/scoped retrieval, ordinary
container restart, backup and an isolated reader-role restore have been checked
on Lenomila; see [the dated verification record](../../docs/ARCHIVE_ACCEPTANCE.md).
That record concerns the canonical archive. The enrichment images, public MCP
access, host reboot and offsite recovery need their own deployment verification.
The application can also be tested against a local PostgreSQL instance.
The image pins Python **3.12.14** and installs the checked-in
`requirements-archive-lock.txt` before installing the application without
dependency resolution. The enrichment extra pins ONNX Runtime 1.23.2,
tokenizers 0.22.1 and NumPy 2.3.4. Cognee has its own locked CPU environment;
its dependencies never replace the MCP interpreter's packages. Dockerfile-specific
ignore files exclude private configuration and export data from build contexts.

## Host qualification

The initial deployment targets Lenomila's native Ubuntu host with Docker Engine
and Compose v2.17 or newer. Named build contexts let the Cognee image reuse the
locally built archive image. Keep PostgreSQL and archive volumes on its Linux filesystem.
Before enabling the services, verify the actual host identity, startup and shutdown behavior,
available RAM and disk, sustained disk writes, existing workload headroom, and
both chosen bridge subnets. Do not infer Lenomila's capacity from Amsterdam's PA
host. Qualify the host's sleep and reboot settings for continuous operation.

The starting continuous-service limits are:

| Service | Memory cap | CPU cap | Purpose |
| --- | ---: | ---: | --- |
| `db` | 1 GiB | 1 | Canonical records, durable jobs, vectors and derivations |
| `watch` | 256 MiB | 0.25 | Finalized export discovery |
| `worker` | 1 GiB | 1 | Streaming canonical imports |
| `mcp` | 2 GiB | 1 | Scoped retrieval and one local query encoder |
| `embedding` | 2 GiB | 2 | Multilingual E5 inference, batch four |
| `knowledge` | 512 MiB | 0.5 | Extractive summaries and decision candidates |
| `cognee` | 6 GiB | 2 | Isolated CPU graph sample and explicit backlog |

Migration and backup jobs each have a temporary 512 MiB limit. Limits are ceilings,
not reserved memory or measured throughput. The CPU-only model workers avoid a
CUDA dependency; a detected GPU does not change this configuration. Leave host
headroom and measure RSS, throughput and disk growth before increasing concurrency.
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
capacity monitoring. Enrichment checks the same reserve before durable work;
public model preparation also checks its model-volume filesystem. These checks
provide headroom, not a quota against other
host processes; verify free space and resolve low-space receipts before retrying.
Malformed reserve configuration fails worker, dispatch and watcher startup before
pending imports can be misclassified as invalid source data.

## Prepare private configuration

From this directory:

```bash
umask 077
# This public script is bind-mounted and read by PostgreSQL UID 999.
# A clone made under a private umask can otherwise leave it unreadable.
chmod 755 init-db.sh
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

The additional `enrichment` bridge defaults to `172.30.79.0/24` / `172.30.79.1`
and is internal. Verify it is unused before deployment. PostgreSQL joins both
bridges; `embedding`, `knowledge` and `cognee` join only the internal bridge.
The MCP service stays on the original `archive` bridge, preserving its verified
proxy peer. The public-artifact preparation services use the ordinary archive
bridge, but have no database DSN, conversation volume, ACL or OAuth mount.

## Start and check

From this directory, after configuration and host qualification, prepare the
images and public model assets explicitly. This sequence is for a **new empty
database**; use the following upgrade section for an existing archive.

```bash
docker compose config --quiet
docker compose build db mcp cognee
docker compose --profile maintenance run --rm --no-deps prepare-e5
docker compose --profile maintenance run --rm --no-deps prepare-cognee
docker compose up -d
docker compose ps
docker compose logs --tail 100 migrate watch worker mcp embedding knowledge cognee
```

`prepare-e5` downloads only the pinned multilingual E5 ONNX model and tokenizer.
`prepare-cognee` downloads only its separately pinned public GLiNER and English
embedding assets. Both verify the release-pinned hashes. They share the separate
`archive_models` named volume and receive no historical conversations or database
credentials. Runtime containers mount the model volume read-only; workers and
MCP never download a model during a query or import. The Cognee preparation
profile uses a temporary private directory for its small request and response.

The one-time PostgreSQL initialization creates a non-superuser ingest role and
a separate reader role. The reader has SELECT access to the `aster_archive`
schema and defaults to read-only transactions; it has no queue-table privileges.
The `migrate` service creates/upgrades archive and Procrastinate tables before
other application services start. With `ASTER_ARCHIVE_ENABLE_ENRICHMENT=1`, it
also applies separately checksummed embedding and knowledge migrations; it
does not alter the canonical v1 migration or create the PostgreSQL extension.
The derived database image installs pgvector at commit
`f37c13f68b57d2c3472b2214fbcff699d6d34876` with portable `OPTFLAGS=''` and enables
the extension during fresh initialization. Initialization scripts run only for a new
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

### Add enrichment to an existing archive

Take and verify a backup first. Build the three images and prepare the two model
sets with the commands above while the existing canonical service remains intact.
Stop the archive writers and reader for the database image change, retaining
the existing named volumes:

```bash
docker compose stop watch worker embedding knowledge cognee mcp
docker compose up -d --no-deps db
docker compose ps db
```

Wait for the database to report healthy. Its existing volume skips initialization
scripts, so use the authorized local PostgreSQL operator to install the extension
and then run the application migration:

```bash
docker compose exec -T db psql -U postgres -d aster_archive \
  --set ON_ERROR_STOP=1 --command 'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;'
docker compose run --rm --no-deps migrate
docker compose up -d watch worker mcp embedding knowledge cognee
```

Keep both the Compose project name and `archive_database:/var/lib/postgresql`
mount unchanged. No data-directory conversion or `down -v` is part of this upgrade.
The ingest role remains a non-superuser; only the local operator installs the
extension. Verify a synthetic semantic query, exact fetch and scope denial before
opening new retrieval access. If a model is absent or fails verification, canonical
lexical retrieval remains available and reports its enrichment fallback.

### Inspect status

Use the CLI's status command with the same owner and source identifiers used in
`.env`:

```bash
docker compose exec worker python -m aster_archive status \
  --owner owner-personal --source chatgpt-personal
```

Inspect import states, record counts, error codes, and coverage. An idle process
or a healthy PostgreSQL socket does not prove that an export was fully imported.
This release does not include an HTTP health endpoint or an external monitor.

### Enrichment receipts and Cognee expansion

The `embedding` worker deduplicates exact passage content within an owner and
model generation before CPU inference. It resumes durable jobs and covers long
text using bounded token windows. PostgreSQL initially computes exact cosine
similarity over the authorized current passages; no approximate vector index is
assumed. `knowledge` derives cited extractive summaries and unverified decision
candidates from current revisions, preserving older history separately.

Cognee initially processes **five passages** and then remains alive and idle. Its
graph and private replay receipts live under `archive_objects/cognee`; the main
database records scoped citations, job states and measured processing evidence.
Inspect the five completed records and their exact canonical citations. Once the
sample has at least five completions and zero failures, explicitly set
`ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG=1` in the private `.env` and recreate only
that worker:

```bash
docker compose up -d --force-recreate cognee
```

The worker does not expand from sample to backlog by itself. Invalid toggle values
fail startup. Cognee's English embedding pilot is a separate derivative and does
not replace multilingual E5 retrieval. See [embedding design and limits](../../docs/archive-embeddings.md)
and [knowledge/Cognee provenance](../../docs/archive-knowledge.md).

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

### Create the ChatGPT web connection

After HTTPS routing is verified, add the existing archive server directly in
ChatGPT web:

1. Open **Plugins** in the intended account or workspace.
2. Select **+ → Add custom MCP server**.
3. Enter **Aster Conversation Archive** as the name. Under **Connection**, use
   the verified public endpoint `https://YOUR-ARCHIVE-ORIGIN/mcp`.
4. Select **OAuth** with **dynamic client registration (DCR)**. This server uses
   DCR; it does not require a manually supplied static client ID or client secret.
5. Review the server warning, acknowledge it, and select **Create as a plugin**.
   Complete the OAuth owner pairing below when prompted, then review the eight
   discovered read-only tools. Install the resulting plugin and complete its
   account connection if prompted.

The current Plugins flow does not require Developer Mode. See OpenAI's
[custom MCP connection guide](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt),
[connection and testing guide](https://developers.openai.com/plugins/deploy/connect-chatgpt),
and [authentication options](https://developers.openai.com/api/docs/guides/custom-mcp-server).

The standalone package in
[`plugins/aster-conversation-archive`](../../plugins/aster-conversation-archive/README.md)
is for compatible local or desktop runtimes. Saving or uploading its ZIP with
Plugin Creator stores a package; it does **not** create the ChatGPT web App
binding or complete OAuth. A desktop-only package listing without an account
connection is not completion of this web setup.

### Pair and grant the exact client

This service reuses Aster's OAuth implementation and its current `system:admin`
scope name, but exposes only the archive's eight read tools. It contains no
command, file-upload, raw-SQL, or Coordinator-write MCP tools.

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

Use explicit source/project ID lists for scoped reader access. Empty `clients`
denies everyone; a missing client or an unclassified project does not acquire
scope by inference. Do not modify the machine executor's administrator allowlist
to enable archive access. Recreate the `mcp` service after changing its ACL so
configuration reloading is explicit.

Setup is complete only after the intended ChatGPT account is connected and its
tools work in a fresh conversation. Select the installed archive plugin, call
`archive_coverage`, search a known synthetic phrase, fetch its exact passage,
confirm citation and source IDs, and check context. Then confirm an unauthorized
source/client is denied. A saved package, successful tool scan, or OAuth success
alone does not establish archive read access. Canonical passage URLs
require the same authorization; a normal browser click without a bearer token
currently returns 401. No anonymous conversation transcript is published.

Canonical `search`, `fetch` and `archive_context` remain compatible. With a verified
query model, `search` combines lexical and semantic results and reports partial
embedding coverage; a completely quoted query retains literal exact-match
semantics. `archive_search` offers explicit lexical, semantic or hybrid modes.
`archive_knowledge_search`, `archive_knowledge_fetch` and `archive_decision_history`
expose labeled derivatives with bounded canonical passage citations.
`archive_coverage` reports actual component states and unfinished work. Every
read reapplies the authenticated client owner/source/project scope and tombstones.

## Back up and qualify restoration

Run the repository's helper from an authorized local operator terminal:

```bash
../../scripts/archive-backup.sh /srv/aster-archive-backups
```

The helper briefly stops archive `watch`, `worker`, `embedding`, `knowledge` and
`cognee` services that were running,
takes a consistent PostgreSQL custom-format dump, snapshots the live OAuth SQLite
database using SQLite's backup API, copies original archive objects and the client
ACL, records the private deployment environment, and computes SHA256 checksums.
The archive object tree includes Cognee graph state and replay receipts. Public
reproducible assets in `archive_models` are prepared again from the pinned manifest
when restoring, rather than treated as conversation data.
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
checksums; initialize the matching database roles and install the pinned pgvector
extension as a local database operator; restore `database.dump` with
`pg_restore --no-owner --no-acl --role=aster_archive_ingest` into an empty archive
database; restore archive objects, auth snapshot, and client ACL with UID 10001
ownership and private modes; then check counts, exact passages, repeat import,
and job reconciliation before enabling the watcher. Restore OAuth state only to
the intended service identity; a test environment must not reuse live public
routing or accidentally accept production tokens. Do not overwrite the active
archive to test a backup.

## Present scope and remaining qualification

The canonical archive preserves exported conversations, branches and revisions.
The enrichment workers add multilingual embeddings, scoped hybrid retrieval,
cited extractive summaries, decision candidates and an isolated Cognee graph
pilot with separate durable receipts. Derived claims remain distinguishable
from the exact original passages. OCR/transcription and broader semantic graph
quality work remain separate features. There are no paid model calls in this
deployment, and runtime processing does not transfer the corpus to a model API.

Before reporting Lenomila operational, verify: the actual Linux runtime and
resources; finalized export receipt; successful parsing and coverage; repeat
import; interrupted-worker recovery; restart after host reboot; authenticated
MCP retrieval and access denial; an isolated backup restoration; and the
independent offsite copy. These are deployment checks, not reasons to delay local
development while the requested export is being prepared.

References: [PostgreSQL image storage and initialization](https://github.com/docker-library/docs/blob/master/postgres/README.md),
[pgvector 0.8.7 source](https://github.com/pgvector/pgvector/tree/f37c13f68b57d2c3472b2214fbcff699d6d34876),
[Compose named build contexts](https://docs.docker.com/reference/compose-file/build/#additional_contexts),
[PostgreSQL logical backups](https://www.postgresql.org/docs/current/backup-dump.html),
[Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).
