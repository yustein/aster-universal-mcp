# Conversation archive verification

Date: 26 October 2026. This record separates initial development tests from
subsequent native Linux deployment qualification. A fresh public ChatGPT archive
connection and full-account coverage remain separate acceptance gates.

## Combined regression result

The initial combined command used an isolated, synthetic-only PostgreSQL database:

```text
ASTER_ARCHIVE_TEST_DSN=<isolated PostgreSQL DSN> .venv/bin/python -m pytest -q
284 passed, 23 skipped, 7 subtests passed in 19.00s
```

The skips require native Windows, an explicitly selected disposable systemd
machine, or protected operator-test paths. They are not passed deployment gates.
PostgreSQL-dependent archive tests ran rather than skipping. Separate focused
test runs below overlap this combined result and must not be added to it.

The environment was Python 3.12.14 on Ubuntu 24.04 with PostgreSQL 18.6. The
PostgreSQL source archive matched the checksum published by PostgreSQL, and its
test cluster used a private Unix socket with no TCP listener. Network namespace
restrictions required the approved local test context to reach that socket.
Neither production databases nor real ChatGPT exports were used.

An earlier combined run had one test-fixture failure: persistent disposable
outbox rows exceeded the dispatcher's intentional 100-row batch. The fixture now
drains earlier delivery obligations before registering the one-batch test
subject; no canonical records are removed, and production dispatch remains
bounded. All 13 queue tests then passed, followed by the full combined result
above.

## Verified behavior

- Source acquisition preserves exact original bytes, rejects changing inputs and
  corrupt reused objects, and never extracts ZIP-controlled paths.
- Streaming parsing rejects unsupported/mixed/gapped shard layouts, duplicate
  paths/JSON keys, links, excessive expansion, corrupted conversation members
  and oversized records.
- Normalization retains graph branches and revisions, exact text parts, raw
  metadata and opaque asset references. Unsupported/internal content does not
  appear as ordinary searchable text.
- Real PostgreSQL tests verify idempotency, stable historical fetch, older
  snapshot handling, chronology ambiguity, owner/source/project isolation,
  branch-correct pagination, tombstones, explicit parse/data gaps and restoration
  of a missing source object before resumption.
- An injected failure immediately before the checkpoint rolls back the
  revision, passages and held derivations together. An interruption after a
  committed conversation resumes without duplicating it.
- Overlapping passage windows preserve exact offsets and reconstruct long
  source text. Quoted phrases and English/Turkish terms crossing a passage cut
  remain retrievable.
- The watcher waits for stable scans, ignores unfinished files, reuses durable
  receipts after restart, detects equal-size/mtime replacements and recovers
  from registration-before-receipt interruption.
- Actual Procrastinate connection dispatch rolls back enqueue and outbox ack
  together. A real queue worker launched an importer subprocess and completed a
  synthetic snapshot. Heartbeat rechecks protect live jobs; stale/exhausted jobs
  leave recovery/dead-letter receipts.
- Graceful shutdown remains resumable. A real Linux SIGKILL test confirms the
  importer child stops with its worker; the pre-guard parent-exit race is also
  rejected.
- MCP tests use the actual SDK and ASGI composition to check search/fetch shapes,
  read-only annotations, authorization, revocation, canonical URL protection,
  escaped HTML, rate/response limits and sanitized errors.

## Packaging and database roles

The development wheel built successfully. Installed outside the checkout, all
ten functional archive modules imported from the wheel, and their bytes plus
`schema.sql` matched the source. The build emitted the existing setuptools
deprecation warning about the project's license-table syntax; no release tag or
published release asset was created.

The deployment initializer was exercised against a separate PostgreSQL 18.6 test
database. Migration succeeded as the non-superuser ingest role, and status worked
as the read-only role. The reader could SELECT the archive schema, could not
write it, and could not SELECT a public queue probe table. The PostgreSQL 18
volume layout uses `/var/lib/postgresql` with its version-specific PGDATA.

The backup helper's synthetic check preserved original/config bytes and recovered
a committed WAL-only SQLite row through the SQLite online-backup API. This was
not a complete Docker backup-and-restore qualification or an offsite copy.

The local OAuth operator wrapper passed six focused tests, included in the full
result. An actual subprocess constrained to DAC_OVERRIDE/FOWNER/CHOWN respected
the existing root guards and restored private UID/GID10001 ownership. A UID10001
process could subsequently reopen and write the SQLite database. Error cleanup,
symlink rejection, allowed operations and shell syntax were checked.

The Dockerfile pins Python 3.12.14 and the complete runtime dependency lock.
Compose syntax/structure and shell scripts were checked, but Docker was not
available in the development environment. The native deployment checks below
subsequently exercised the built image and actual container lifecycle.

## Storage reserve follow-up

After the combined result above, a storage headroom guard was added for native
Lenomila deployment. Compose reserves 10 GiB; source acquisition requires this
reserve plus the complete incoming file size, and ingestion checks again before
each conversation/checkpoint transaction. Capacity failures remain retryable I/O
errors. Worker, dispatch and watcher startup reject malformed reserve settings
before they can classify valid pending data as an invalid export.

The initial guard's selected source/storage/import regression run passed 70 tests
in 2.71 seconds. After the startup-validation refinement, the storage, queue and
watcher suites passed 30 tests in 0.93 seconds against the same disposable
PostgreSQL 18 database. These overlapping focused results are separate from the
earlier combined suite; they are not an additional full-suite result.

The real-database checks preserve a committed checkpoint through low space and
resume without duplicate revisions. They also verify that malformed reserve
configuration leaves a valid import pending, with no queue acknowledgement,
queued job, quarantine receipt or dead letter. The guard measures the archive
filesystem and does not reserve space against other host processes or monitor a
database placed on another filesystem.

## Native deployment qualification

The deployment was exercised on native Ubuntu 24.04.4 LTS with Docker Engine
29.1.3 and Compose 2.40.3. The application image used Python 3.12.14 and the pinned
runtime lock; PostgreSQL ran from `postgres:18.6-bookworm`. No ingestion VM is
involved. The qualified implementation commit is
`84b1c3aaf26fc086476347a27e279bc6a913008c`; its built application image is
`sha256:8e262cddb52c4912d64282b7aa998de5b7d19745696da944761ee9c5a1a7c3c8`.

Two packaging defects were found and corrected during these checks:

- A private clone umask made the bind-mounted PostgreSQL initializer unreadable
  by the database user. The runbook now sets the public script to mode 0755.
  Initialization applies roles, schema ownership and grants in one transaction;
  readiness requires the final TCP listener and the initialized roles/schema.
  The original incomplete, empty database was repaired only after verifying
  that both application roles, the archive schema and all user tables were
  absent. A subsequent separate, empty container/volume verified fresh atomic
  initialization without manual repair, then was removed.
- The deployment helper named `operator.py` shadowed Python's standard-library
  module during a cold backup process. It was renamed `archive_operator.py`
  without changing its authority checks. Actual-script subprocess regressions
  run with `-E -S -B`, preventing site initialization from hiding the collision.
  The focused backup/operator suites passed **8 tests in 0.20 seconds**, with
  no skips. Both corrected entrypoints were then exercised in the built image.

The following native synthetic checks passed:

- The deployed durable worker completed one queued conversation with two answer
  branches. Exact English/Turkish text, original SHA256, branch context,
  owner-scope denial, stable passage identity and repeat-import idempotency were
  checked. The fixture had one conversation, one revision and three passages
  under a dedicated qualification owner; personal-source coverage stayed zero.
- A normal stop/start of the archive writer services and restart of PostgreSQL
  preserved the same exact passage and original. This was a container restart,
  not a host reboot or an injected crash during a real import.
- The actual backup script produced a PostgreSQL custom-format dump, original
  objects, a consistent OAuth SQLite snapshot, the client ACL, private deployment
  configuration and checksums. A deliberately separate database and three new
  volumes received the restore. Verification used the restricted reader role
  and confirmed the exact passage, original SHA256, branch/scope checks, SQLite
  integrity and empty-deny ACL. The disposable restore targets were removed.
- Only the synthetic conversation was then tombstoned. Its small, explicitly
  tagged receipts remain, and it never contributes to personal-source counts.

Docker is enabled at host startup and the persistent services use
`restart: unless-stopped`. Existing host power settings were checked: automatic
AC sleep and lid-triggered suspend were disabled, and sleep/hibernate targets
were masked. These observations do not prove recovery after a full host reboot,
power loss, or network outage.

The database has no published host port. OAuth state was initialized offline
with an empty client ACL; no public archive listener, DNS/tunnel change, owner
pairing, or fresh ChatGPT MCP connection was part of this qualification.
Private source-transfer and real-import receipts remain on the target host;
personal conversation content, identifiers and corpus inventories are not
included in this public verification record.

## Limits on this acceptance

This establishes the tested development behavior and the native synthetic
deployment/restore path. It does not establish actual full-account coverage,
corpus throughput or a capacity envelope, live public OAuth/MCP pairing,
native-host reboot recovery, an offsite copy, or restoration of an entire real
corpus. The newly requested export has not been consumed by this verification.

Embeddings, pgvector indexes, evidence-linked knowledge extraction, accepted
decision reconciliation and attachment processing remain held/later stages. No
paid model request occurred. See [the implementation contract](CONVERSATION_ARCHIVE.md)
and [deployment procedure](../deploy/archive/README.md) for the actual scope.
