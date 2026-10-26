# Local archive knowledge and isolated Cognee processing

## What the records mean

The canonical archive remains authoritative evidence. This additive layer stores three different record types:

- `summary`: an **extractive selection of original quotes**, with first/latest messages by role and evenly spaced messages on the selected source branch. Each quote is at most 400 Unicode characters. Selection counts, omissions, branch problems, and exact source offsets are returned. This is not a model-written narrative.
- `decision_candidate`: a source sentence matching a limited English/Turkish decision-language pattern. Exact user approval/direction language is distinguished from assistant suggestions and general discussion. Every automatic record has `verified_decision=false`. A bare “yes” never approves an earlier suggestion automatically; quotes and fenced code do not establish approval. Alternative branches and original timestamps are retained.
- `cognee_summary`: entities and relation text inferred by the local GLiNER demo model, stored with the exact input passage citation. These are unverified model outputs, not verified decisions or an LLM-written narrative.

The first two kinds preserve source text in every language. Candidate classification has deliberately limited English/Turkish pattern coverage. The Cognee pilot uses **English BGE embeddings**, separately labeled from the multilingual E5 archive search. Do not present its recall quality as multilingual validation.

## Isolation and retrieval

`KnowledgeStore` always joins derived rows to the canonical conversations at read time. Owner, stable source ID, current project classification and tombstones apply to every search, fetch, coverage and history call. Search defaults to the selected current revision; historical exact fetch and explicit decision history retain revision metadata. Deleted conversations disappear from all these reads immediately. Physically purging canonical or derived storage is a separate retention operation; a tombstone is not a claim that backup bytes were erased.

The knowledge MCP wrapper must obtain `ArchiveScope` from the authenticated protected grant, never from model-supplied owner/source/project parameters. A citation contains `passage_id`, `revision_id`, `node_id`, `part_index`, exact quote, author role and Unicode offsets. Build canonical URLs with the same origin and `/archive/passages/{passage_id}` route as original archive retrieval. Derived text is historical evidence and confers no execution authority.

The Cognee child receives no archive DSN, OAuth material, cloud Cognee configuration or paid model API key. It runs in a separate interpreter with `-I` to prevent local modules shadowing the standard library. Its private, deliberately empty `COGNEE_ENV_FILE` overrides Cognee's default `.env` discovery. Only the explicit preparation operation can use network transport settings. During processing, Hugging Face and Transformers offline modes plus a Python socket audit guard reject outbound TCP/DNS; URL/file-like historical text is prefixed as plain text and HTTP loaders are disabled. There is no exposed Cognee server, cloud connection, raw SQL/Cypher tool or `prune` call.

Each immutable conversation revision gets a separate graph/data/system directory under a hash of owner/source/revision/pipeline, and each source passage a dataset. Existing cloud/books datasets are never selected. There is no cross-conversation graph merge in this first local pilot. Derived records provide scoped cross-conversation retrieval in PostgreSQL.

## Migration and deterministic derivation

Keep `schema.sql` and its v1 checksum unchanged. The shared additive migrator installs `knowledge_schema.sql` as component `knowledge`, version 1; migration hashes are recorded independently.

The ordinary archive environment needs no new package for extractive derivation:

```bash
python -m aster_archive.knowledge migrate
python -m aster_archive.knowledge derive --owner OWNER --source SOURCE --archive-root /var/lib/aster-archive --limit 10
python -m aster_archive.knowledge status --owner OWNER --source SOURCE
python -m aster_archive.knowledge search --owner OWNER --source SOURCE --query 'Coordinator PostgreSQL'
python -m aster_archive.knowledge history --owner OWNER --source SOURCE --conversation CONVERSATION_ID
```

`ASTER_ARCHIVE_DATABASE_URL` supplies the writer DSN; do not place it on the command line. `ASTER_ARCHIVE_MIN_FREE_BYTES` is validated before work and checked before consuming an attempt. Each bounded invocation processes up to 10 revisions by default. The supervised worker repeats it while the canonical importer keeps running:

```bash
python -m aster_archive.enrichment_worker knowledge --owner OWNER --source SOURCE --archive-root /var/lib/aster-archive --batch-size 10 --poll-seconds 60
```

Each loop processes one bounded batch and waits for the configured interval. `--once` performs one batch and exits. The worker requires an existing archive directory and the additive knowledge migration before starting. Repeated exports with an unchanged revision do not duplicate records. Old revisions are retained and processed after current revisions.

Source passages are reconstructed by original part offsets before sentence extraction, so overlapping windows do not manufacture an approval or lose a code fence. Revisions over 10,000 nodes or passages, or over 5,000 decision candidates, receive an explicit failed budget receipt rather than a partial summary advertised as complete. The per-revision node read extracts only timestamps, not full raw message JSON.

Jobs are versioned by revision and pipeline key. A PostgreSQL session advisory lock serializes one owner/source/pipeline worker. On process death the lock is released and its `running` receipt can resume; records and the completion checkpoint commit together. Three interrupted attempts exhaust the receipt. Permanent source/budget failures remain failed; operators inspect them before requeueing. No traceback or original message is persisted as a job error.

## Isolated CPU Cognee runtime

Pinned artifacts live in `deploy/archive/cognee/`:

- `requirements-lock.txt`: Python 3.12 Linux x86_64; Cognee 1.6.3, GLiNER2 2.1.0, CPU PyTorch 2.8.0, fastembed 0.8.0 and resolved dependencies.
- `model-manifest.json`: verified model files and SHA-256 values. GLiNER snapshot `54785d51df8d86a0d8f2eb212fec15871aad2c4d`; quantized BGE ONNX snapshot `aa8f8b060edb00e03bfdd08813a2949946c8ba55`. An identical `aster_archive/cognee-model-manifest.json` ships in the installed package and is read through `importlib.resources`; a repository checkout is not required at runtime.
- `Dockerfile`: optional image layered over the ordinary archive image, keeping Cognee in `/opt/cognee-venv` and archive/MCP dependencies in the original environment.

For a native service, install the separate interpreter without changing the archive environment:

```bash
python3.12 -m venv /opt/aster-archive/cognee-venv
uv pip install --python /opt/aster-archive/cognee-venv/bin/python --torch-backend cpu -r deploy/archive/cognee/requirements-lock.txt
```

The `--torch-backend cpu` flag matters because the lock pins a `+cpu` build. Do not substitute a generic CUDA installation. The tested environment used approximately 2.0 GiB for installed packages and 811 MiB for prepared models, excluding package caches and graph growth.

Configure absolute dedicated paths:

```bash
export ASTER_ARCHIVE_COGNEE_PYTHON=/opt/aster-archive/cognee-venv/bin/python
export ASTER_ARCHIVE_COGNEE_ROOT=/var/lib/aster-archive/cognee
export ASTER_ARCHIVE_COGNEE_MODEL_ROOT=/var/lib/aster-archive-models/cognee
python -m aster_archive.cognee_bridge prepare
```

The graph/receipt and model paths above are the defaults; the isolated Python interpreter must be configured explicitly. The rebuildable model cache stays outside the canonical archive directory because Hugging Face snapshots contain symlinks. Mutable SQLite/Lance/graph state and durable child receipts remain under `/var/lib/aster-archive/cognee`; stop its writer along with the other archive writers before filesystem backups.

Preparation downloads only the two pinned public model snapshots; no archive content is passed to it. It verifies the downloads against the packaged release manifest before writing an atomic cache manifest. Each processing child compares that cache manifest with the trusted packaged copy supplied through its private request and then verifies every model file. Editing the cache's own hashes cannot authorize replacement weights. Processing cannot fetch missing weights or silently install runtime packages.

Use a dedicated low-priority service/container with **two CPU cores, six GiB memory and one active Cognee child**. The parent checks child RSS every second and kills it when the configured budget is crossed; the child has a 12 GiB virtual address-space ceiling for library/mmap allocations. Supply a six GiB cgroup cap in production for hard memory enforcement. The graph buffer is 256 MiB, graph address-space maximum two GiB, model batch size one, chunk budget 256 tokens and GLiNER inference concurrency one. Default wall-clock task timeout is 900 seconds. The child receives parent-death SIGTERM on Linux and verifies its expected parent before processing; ordinary timeout/interruption terminates the process group.

## Sample, checkpoint and backlog

```bash
python -m aster_archive.cognee_bridge run --owner OWNER --source SOURCE --archive-root /var/lib/aster-archive --limit 2
python -m aster_archive.cognee_bridge status --owner OWNER --source SOURCE
```

Repeat the bounded run to process a maximum of **five sample passages**. The reproducible sample balances user/assistant passages across recent revisions. It is not a statistical estimate of the whole archive and does not establish non-English quality. Inspect its cited results, failure count, runtime, memory and relation accuracy before expanding. A local model can invent an incorrect relationship even when graph processing succeeds.

For a supervised sample service, use:

```bash
python -m aster_archive.enrichment_worker cognee --owner OWNER --source SOURCE --archive-root /var/lib/aster-archive --batch-size 2 --poll-seconds 60
```

At the sample cap the process remains alive, emits an `idle` count and waits. It never unlocks the backlog automatically, including when a new export arrives. `--once` exits after one bounded batch. The worker validates the model manifest and storage setting before it can consume a pending job.

After that review, the explicit `--enable-backlog` mode seeds successive bounded batches. It enforces at least five completed sample passages and zero failed jobs as mechanical prerequisites; that check is not a substitute for inspecting quality.

```bash
python -m aster_archive.cognee_bridge run --owner OWNER --source SOURCE --archive-root /var/lib/aster-archive --limit 2 --enable-backlog
python -m aster_archive.enrichment_worker cognee --owner OWNER --source SOURCE --archive-root /var/lib/aster-archive --batch-size 2 --poll-seconds 60 --enable-backlog
```

The supervised worker also accepts the explicit environment setting `ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG=1`. Its default is `0`; any value other than `0` or `1` fails startup before jobs are touched. The CLI flag or that environment setting enables the same five-complete/zero-failed gate. Sources with fewer than five eligible passages cannot pass this initial backlog gate.

Graph processing writes a private completed child receipt before returning. If the PostgreSQL acknowledgement fails afterward, retry reuses the matching receipt instead of recreating the graph. PostgreSQL records and job completion are one transaction; stable IDs make replay idempotent. Child success with no summary is counted explicitly as zero summaries, not filled with invented text. Failures retry at most three attempts and retain a sanitized failure receipt. Persistent model/storage problems require operator recovery; there is no infinite automatic retry.

Both supervised workers handle SIGTERM/SIGINT by unwinding the active batch and releasing its session lock. An active Cognee child receives process-group termination; its completed child receipt remains available for restart. An interrupted job retains its durable attempt and can resume within the existing three-attempt budget. Progress logs contain only the component/mode, counts and fixed error codes; they contain no conversation text, owner/source names, SQL, database URLs or raw exceptions. Operational failures wait for the next poll, without adding another job retry mechanism. Counts report work acknowledged by this worker invocation, not total archive coverage; the existing `status` commands read durable coverage.

The child never executes instructions from the historical passage. Only fixed extraction/retrieval code is invoked, using a fixed archive entity/relation schema. Model output stays unverified and cannot modify decision acceptance or canonical text.

## Python interfaces for MCP integration

```python
knowledge = KnowledgeStore(canonical_store)
knowledge.search(scope, query, kind=None, limit=20)
knowledge.fetch(scope, record_id)
knowledge.history(scope, conversation_id, limit=20, cursor=None)
knowledge.coverage(scope)
```

Results contain `id`, `title`, `text`, `kind`, `metadata`, and `citations`. `history` returns `records`, `next_cursor`, ordering semantics and a warning that candidates are not an approved decision ledger. Every history page rechecks scope. Scope-bound cursors cannot grant access to a different owner/source/project set. Both search and history are bounded to 20 records per page.

## Verification and upstream basis

The focused tests use synthetic unique owner/source namespaces on real PostgreSQL. They cover authorization, tombstones, historical/current revisions, original offsets, source branches, approval language, sample bounds, bounded failure receipts and crash replay. The optional real CPU test runs Cognee, interrupts after the child succeeds but before DB acknowledgement, and proves replay creates one set of scoped derived records.

The final local focused gate passed **22 tests in 24.86 seconds**, including real CPU Cognee processing and PostgreSQL crash replay. The synthetic inference receipt recorded 20.542 seconds and peak observed RSS of 2,227,347,456 bytes (about 2.07 GiB), with one chunk, three nodes, six candidate/four kept edges, and one derived summary. These measurements came from the local test environment, not native Lenomila, and do not establish whole-archive throughput or semantic accuracy.

The later supervised-worker/manifest gate passed **27 tests in 30.60 seconds**, including PostgreSQL restart recovery, the five-passage cap, SIGTERM cleanup, private error reporting, manifest tampering rejection and another real CPU Cognee checkpoint/replay run. This was a focused follow-up, not a repeat of the entire archive suite.

Relevant primary sources inspected alongside the pinned wheel:

- [Cognee 1.6.3 distribution](https://pypi.org/project/cognee/1.6.3/)
- [Recall without an LLM key](https://docs.cognee.ai/guides/no-llm-remember-recall)
- [Custom GLiNER extraction](https://docs.cognee.ai/guides/gliner-llm-free-cognify)
- [Cognee source](https://github.com/topoteretes/cognee)
- [GLiNER model](https://huggingface.co/fastino/gliner2.5-base-v1)
- [BGE ONNX model](https://huggingface.co/qdrant/bge-small-en-v1.5-onnx-q)

The Cognee 1.6.3 wheel SHA-256 inspected during implementation was `b86ae1865f7ba50c197c9aab598f793a56ac8f458a7df5caaa7e973375646aaa`. Its local GLiNER path is explicitly labeled a demo by upstream. No production-quality or verified-decision claim follows from successful execution.
