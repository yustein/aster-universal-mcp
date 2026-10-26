# Conversation archive development verification

Date: 26 October 2026. This record covers the first archive implementation in
source, before live Lenomila deployment or a new ChatGPT archive connection.

## Combined regression result

The final combined command used an isolated, synthetic-only PostgreSQL database:

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
available in the development environment. Image construction, container startup,
reverse-proxy peer identity and full container operation therefore remain
unverified here.

## Limits on this acceptance

This is development evidence for the exact implemented archive behavior. It does
not establish actual full-account coverage, real-export compatibility beyond the
supported synthetic formats, corpus throughput, hardware sizing, live public
OAuth/MCP pairing, host/VM reboot recovery, independent offsite backup or a full
restore. The newly requested export has not been consumed by this verification.

Embeddings, pgvector indexes, evidence-linked knowledge extraction, accepted
decision reconciliation and attachment processing remain held/later stages. No
paid model request occurred. See [the implementation contract](CONVERSATION_ARCHIVE.md)
and [deployment procedure](../deploy/archive/README.md) for the actual scope.
