# Local archive embeddings

This component adds a reproducible semantic index to the canonical archive.
Original export bytes, passage text, branch structure and citation offsets remain
canonical. Model vectors only help rank source passages.

## Qualified baseline

- Model: `intfloat/multilingual-e5-small`, revision
  `fd1525a9fd15316a2d503bf26ab031a61d056e98`.
- Official ONNX FP32 artifact SHA-256:
  `ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665`.
- Official tokenizer SHA-256:
  `0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39`.
- Runtime pins: ONNX Runtime 1.23.2, tokenizers 0.22.1, NumPy 2.3.4.
- PostgreSQL 18.6 with pgvector 0.8.7, upstream commit
  `f37c13f68b57d2c3472b2214fbcff699d6d34876`.
- 384 float32 components; normalized vectors; exact cosine search first.
- Two CPU inference threads, one inter-op thread, batch size four. Thread
  spinning is disabled. No GPU, CUDA, hosted embedding service or paid API is
  required.

The publisher documents multilingual retrieval, distinct query/passage prefixes,
attention-masked mean pooling and a 512-token limit. The code verifies immutable
artifact hashes and runtime versions before loading the model. It never loads
remote Python code.

Primary references:

- [Pinned model card](https://huggingface.co/intfloat/multilingual-e5-small/blob/fd1525a9fd15316a2d503bf26ab031a61d056e98/README.md)
- [Pinned ONNX artifact](https://huggingface.co/intfloat/multilingual-e5-small/blob/fd1525a9fd15316a2d503bf26ab031a61d056e98/onnx/model.onnx)
- [Tokenizer artifact](https://huggingface.co/intfloat/multilingual-e5-small/blob/fd1525a9fd15316a2d503bf26ab031a61d056e98/tokenizer.json)
- [ONNX Runtime thread controls](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)
- [Pinned pgvector source](https://github.com/pgvector/pgvector/tree/f37c13f68b57d2c3472b2214fbcff699d6d34876)

## Long passages and fidelity

For ordinary inputs, token IDs exactly match encoding the complete text with
the publisher's `query: ` or `passage: ` prefix and special tokens. If the
input exceeds 512 tokens, every body token is covered by windows, with 64 body
tokens of overlap and the correct prefix repeated for each window. Each window
is attention-mask mean pooled and L2 normalized. Window vectors are averaged
and normalized again to obtain one vector per exact passage hash.

This aggregation is an explicit retrieval choice, not a publisher benchmark
claim. It can dilute a small detail in a longer passage. It does not silently
discard a tail, rewrite text, invent source spans or claim the model saw an
unbounded whole conversation. Stored token/window counts make the segmentation
visible. Original Unicode passage offsets and text SHA-256 are unchanged.

Generation identity includes the model revision, both artifact hashes, runtime
versions, dimensions, prefixes, pooling and window policy. A new generation
cannot reuse vectors from an incompatible model/preprocessing recipe.

## Durable work

The separate `embeddings` component migration is registered in
`aster_archive.enrichment_migrations`; `schema.sql` and its version-1
checksum are unchanged. Install the pgvector extension once with the database
operator role:

```sql
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
```

Then run the component migration with the existing ingest role. Its default
read grants must continue to cover new tables for the MCP reader role.

Jobs are keyed by generation, owner and exact passage text SHA-256. Discovery
deduplicates before inference, including repeated passages across branches,
conversations and changed export revisions. Jobs only select current,
authorized, untombstoned canonical passages. No duplicate text copy is stored
in the job table.

A bounded claim holds row locks only long enough to record its lease and get
its authorized text. Inference runs outside the transaction. Vector publication
and completion receipts share one transaction. Expired leases can be reclaimed;
an obsolete lease token cannot commit after another worker takes over. Five
attempts are allowed before an explicit local retry is required. Failed receipts
store bounded error codes, never model exceptions or source text.

The disk reserve is checked before claims and again before vector publication.
A resource pause releases the jobs without spending their retry budget. A crash
can repeat inference for its unfinished batch, while previously committed hashes
are reused.

## Local operator commands

These are commands inside the configured archive runtime. The database URL is
supplied through its protected environment, never a command-line argument.

```sh
python -m aster_archive.embedding_worker prepare-model --model-dir /var/lib/aster-models/e5-small
python -m aster_archive.embedding_worker migrate
python -m aster_archive.embedding_worker run --owner OWNER --source SOURCE --archive-root /var/lib/aster-archive --model-dir /var/lib/aster-models/e5-small --once
python -m aster_archive.embedding_worker status --owner OWNER --source SOURCE
```

`prepare-model` is the only network-using operation. It fetches two fixed public
URLs into temporary files, verifies their pinned hashes and atomically publishes
them. It refuses an existing corrupt artifact. It never receives conversation
text, authentication tokens or a user-supplied download URL. Reader/worker model
loading only accepts already prepared local files.

Remove `--once` for the continuous worker. It drains bounded work, reconciles
new imports periodically and stops gracefully after the current batch.
`--once` processes one batch, not the entire corpus. After investigating failed
jobs, `retry-failed --owner OWNER --source SOURCE` explicitly resets their retry
budget. Neither mutation is exposed through MCP.

The normal deployment reserve remains 10 GiB through
`ASTER_ARCHIVE_MIN_FREE_BYTES`. If models, archive objects and PostgreSQL use
different filesystems, monitor each filesystem independently.

## Retrieval

`EmbeddingStore.search(scope, query, encoder=encoder, mode="semantic"|"hybrid")`
returns canonical passage IDs and titles, numeric ranks/scores, generation
identity and authorized coverage. Fetch/context retain the existing exact
passage contract.

Every vector query joins canonical conversations and reapplies owner, source,
project, current-revision and tombstone restrictions before ranking. Identical
text in another source/project never supplies an unauthorized passage ID.
Historical vectors may remain for reproducibility, but current search cannot
return their superseded or tombstoned passages.

Semantic mode uses exact cosine distance. Hybrid mode combines up to 200 scoped
lexical and semantic candidates with reciprocal rank fusion (constant 60).
Cosine similarity and fusion scores are ranking values, not probabilities.
Whole queries in double quotes retain exact, case-sensitive lexical phrase
semantics. Coverage reports how much of the currently authorized corpus has
vectors; partial indexing is not represented as complete history.

## Resource and verification notes

For an illustrative 100,000 unique passages, raw 384-dimensional float32 vectors
occupy roughly 146.5 MiB before database row/index/WAL overhead. Deduplication may reduce this.
The two public model artifacts occupy about 487 MB. These are sizing arithmetic,
not measured total PostgreSQL storage.

A local synthetic qualification on Python 3.12.14 measured about 1.13 GiB peak
RSS for one model process and 0.66 seconds for four approximately 451-token
passages. This was **not measured on Lenomila** and is not a full-corpus ETA.
Allocate at least 2 GiB to each process hosting the model, then measure native
throughput. A worker and an MCP process each loading the model have separate
model memory.

Tests use a real disposable PostgreSQL 18.6 database and pgvector 0.8.7, plus
the actual pinned local E5 model for cross-language retrieval. They cover
deduplication, generation separation, transaction interruption, lease fencing,
retry limits, disk pauses, owner/source/project isolation, revision changes,
tombstones, exact quoted Unicode and exact source fetch. Model tests never
download automatically; set `ASTER_ARCHIVE_TEST_MODEL_DIR` to already verified
artifacts and `ASTER_ARCHIVE_TEST_DSN` to the disposable test database.
