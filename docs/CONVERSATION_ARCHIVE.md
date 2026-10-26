# Conversation archive: first implementation

This opt-in service turns supplied ChatGPT exports into a durable conversation
database and an authenticated source for later ChatGPT sessions. It is the first
stage of Aster's shared knowledge system. It makes exact history searchable
before any embedding or language-model processing is enabled.

The archive uses a separate PostgreSQL database, original-file store and OAuth
identity. The Coordinator can refer to archive passage IDs without putting
conversation history into its task ledger. Aster PA conversations can be part of
the owner's archive; access to operational PA/customer databases is a separate
scope and is not part of this importer.

## Implemented components

| Component | Implementation |
|---|---|
| Original snapshots | SHA256-addressed ZIP/JSON files; atomic exclusive publication; integrity check before reuse/restart |
| Streaming reader | Python and ijson 3.6.0; monolithic or numbered conversation JSON shards |
| Canonical database | PostgreSQL 18.6; versioned schema and source-backed conversation/node revisions |
| Lexical retrieval | PostgreSQL GIN indexes with simple, English and Turkish dictionaries |
| Durable jobs | Procrastinate 3.10.0; transactional outbox, bounded retries and heartbeat recovery |
| MCP | Existing Aster OAuth composition and MCP SDK 2.3.0; eight read-only tools |
| Continuous acquisition | Stable-file watcher for an operator-controlled inbox |
| Deployment | Separate Docker Compose services, persistent volumes and resource caps |
| Backup | PostgreSQL logical dump, SQLite online backup, original files and configuration |

Runtime dependencies are pinned in `requirements-archive-lock.txt`. No embedding,
summary, transcription, OCR or paid model call occurs in this version. Derived
embedding and knowledge obligations are recorded as **held** in the same
transaction as each new revision. A later configured pipeline can process them
without replacing source evidence.

## What constitutes an import

The local operator supplies a stable owner ID and a stable source ID for the
ChatGPT account/workspace. The importer never infers either from a title or text.
An import identity includes those IDs, the source archive hash and adapter
version. Reusing the same source ID for subsequent snapshots is essential.

The source reader supports a bare JSON array, a ZIP containing
`conversations.json`, or one unambiguous sequence starting with
`conversations-000.json`. It rejects gaps, duplicate paths, links, unsafe member
names, mixed formats, excessive expansion and duplicate JSON keys. It does not
extract ZIP-supplied filesystem paths. A conversation record is bounded before
ijson constructs the whole object; the default limit is 64 MiB.

The original file is retained before parsing. JSONB holds queryable metadata,
not a byte-for-byte replacement for the original. Asset references and the ZIP
inventory are preserved, but an inventory CRC is only a declaration until that
member has actually been read. Conversation-member CRC verification is recorded
after successful parsing. Independent attachment extraction and verification
remain later work.

## Identity, history and chronology

Conversation IDs and graph node IDs come from the export. All graph nodes,
parent/child edges and the selected current node are preserved. Null-message
roots, alternative replies, edits and regenerations remain distinguishable when
the export contains them. Node metadata and unknown fields remain available in
the protected canonical store and original snapshot.

An unchanged conversation reuses its revision. Changed content creates another
immutable revision and new source occurrences. Exact old passage IDs continue
to resolve after later snapshots. An older source update cannot roll the current
pointer back. Missing conversations in a later snapshot are not interpreted as
deletions.

Import order does not establish source chronology. Conflicting revisions with
equal or unknown update times set an explicit ambiguity flag. A later dated
revision does not erase uncertainty about an undated alternative. Automatic
decision extraction must respect this distinction; repetition and assistant
proposals are not accepted policy.

## Exact text and branch context

Ordinary user/assistant text, code and textual portions of multimodal content
are indexed. Tool execution, internal/reasoning content, and unknown content
types are retained in the original/canonical source, but are not exposed as
ordinary searchable prose. This is a deliberately narrower retrieval surface
than the stored export. Searchable-passage counts are not total-message counts.

Passages contain original strings only, without synthetic role prefixes or
separators. The current passage policy uses 2,000 Unicode characters with a
1,000-character stride. Overlap lets a bounded phrase crossing a cut remain
searchable. Each result includes exact part-local offsets and a text hash;
consumers reconstruct a full part by offsets rather than concatenating overlap.
Rechunking a released corpus requires an explicit derived-index migration.

Ordinary search treats query words as lexical terms. A query wholly enclosed in
double quotes requests an exact, case-sensitive substring. Queries are bounded
to 1,000 characters. Search covers passages in the selected current conversation
revision, including its preserved alternative branches; that does not mean every
hit belongs to the selected current branch. Historical IDs remain fetchable.

`archive_context` follows the selected passage node's **ancestors in the same
revision**, ordered from oldest ancestor toward that passage. Sibling replies
never get flattened into this path. The result includes a revision/scope-bound
cursor, a byte/count limit, and an explicit status for a cycle, missing parent or
ancestry limit. It is not an arbitrary global transcript-order operation.

## Transaction and recovery guarantees

Registration commits the import and its outbox row together. The pinned
Procrastinate connector accepts the existing PostgreSQL connection, so queue
insertion and the outbox acknowledgement also commit together. Tests inject a
failure between those writes and verify rollback.

Each conversation's revision, nodes, exact passages, held derivations and import
checkpoint commit in one transaction. A crash before the checkpoint leaves no
partial canonical conversation. A crash after it causes replay to skip that
record. Re-parsing may restart from the beginning of a JSON member; checkpoints
avoid repeated database work, not the cost of scanning to a saved position.

Only one queued import runs at a time. A separate maintenance slot keeps worker
heartbeats and recovery available. A PostgreSQL session lock also serializes
imports for one source, including direct CLI work. Every write uses that locked
connection; a lost connection is not silently replaced under stale ownership.

Imports have a six-hour job bound, at most three attempts, and 30/60-second
transient backoff. Permanent format errors and exhausted failures leave durable
dead letters. Recovery checks worker heartbeats, then rechecks current ownership
under database locks; it does not retry a job merely because it has run a long
time. Graceful worker shutdown permits bounded retry after its child stops. On
Linux, the child also terminates if its worker is killed, preventing an orphan
import from continuing without the worker's deadline.

Malformed individual conversations produce visible invalid-record receipts and
`complete_with_errors`; a stream/container failure marks the import failed.
PostgreSQL-inexpressible values, such as a JSON NUL character, retain their
original bytes and produce an explicit gap. They are not silently rewritten.

These guarantees concern archive database state. They do not imply exactly-once
external model charges or exactly-once actions by a general-purpose agent.

## MCP tools and permissions

| Tool | Result |
|---|---|
| `search(query)` | Bounded passage IDs, titles and stable HTTPS source URLs |
| `fetch(id)` | Exact passage text, source identity, revision, role and offsets |
| `archive_context(id, limit, cursor)` | Paginated ancestor-path evidence in that revision |
| `archive_coverage()` | Visible corpus counts, source date range, import states and stage limits |
| `archive_search(query, mode, limit)` | Explicit lexical, semantic or hybrid passage retrieval with coverage disclosure |
| `archive_knowledge_search(query, kind, limit)` | Scoped derived summaries and decision candidates with source citations |
| `archive_knowledge_fetch(id)` | One derived record and its checked canonical evidence |
| `archive_decision_history(conversation_id, limit, cursor)` | Candidate history across retained revisions and branches; not verified decisions |

For ChatGPT web, follow the
[direct custom MCP server setup](../deploy/archive/README.md#connect-an-authorized-chatgpt-client):
**Plugins → + → Add custom MCP server**, using the existing HTTPS endpoint and
OAuth DCR. The standalone plugin ZIP targets compatible local or desktop
runtimes; uploading it does not create a ChatGPT web App binding. Confirm the
actual account connection and tool calls before marking setup complete.

The existing OAuth protocol is reused with a dedicated archive origin/database.
Its current scope name is `system:admin`, but this service registers only archive
read tools. Exact client IDs also require a protected local grant mapping to an
owner and allowed source/project IDs. Missing grants fail closed. Scope is
reloaded on every call and is never chosen through tool arguments. The deployed
MCP database role is read-only and cannot access the job queue.

Search, exact fetch, context and coverage enforce the same owner/source/project
filters. Project classification is currently unset unless an operator supplies
authoritative classification through a future workflow. A worker grant to a
project list therefore does not include unclassified records. Owner wildcard
access must be explicit.

Canonical `/archive/passages/{id}` URLs independently verify authentication,
token audience/revocation and the same grant. Responses prohibit caching and
escape source text. A normal browser without a bearer token currently receives
401; an interactive browser sign-in/viewer is not implemented. Source URLs never
publish an anonymous transcript. Archived instructions are evidence, not fresh
authorization for an action.

## Operator commands and continuous delivery

Install in an isolated environment, set `ASTER_ARCHIVE_DATABASE_URL`, and run:

```bash
python -m aster_archive migrate
python -m aster_archive import /imports/export.zip \
  --archive-root /var/lib/aster-archive --owner owner-personal --source chatgpt-personal
python -m aster_archive status --owner owner-personal --source chatgpt-personal
```

For continuous operation, start `watch` and `worker` as separate services using
the [deployment instructions](../deploy/archive/README.md). Download to a
`.partial` name, then atomically rename the complete file to `.zip` in the inbox.
The watcher requires two stable scans, then the source copy independently checks
stability. Durable receipts survive watcher restarts. Replacing an existing name
with equal size/mtime is still detected through filesystem identity and ctime.

The local `tombstone` command suppresses a conversation from retrieval and future
reimports. It retains the raw ZIP and old database rows; it is not a physical
erasure tool. Restoring a pre-deletion backup requires replaying the latest
deletion decisions before serving or importing. No deletion is inferred from
absence in an export.

## Coverage and later stages

Coverage is relative to the snapshots actually supplied. The total available
account history remains unknown; the service never labels an import as proof
that every conversation has been captured. An import's observation time is
separate from the timestamps embedded in its source records.

The deployment target is Lenomila's native Ubuntu host. The following remain
distinct next stages: real-export qualification, measured host throughput and
reboot behavior, new ChatGPT connection acceptance,
isolated backup restoration and offsite retention, pgvector/embedding evaluation,
evidence-linked knowledge extraction, current-decision reconciliation, attachment
processing and a human archive browser. No external account export API or live
ChatGPT history feed is assumed. Regularly supplied exports reconcile through
the same stable source identity.
