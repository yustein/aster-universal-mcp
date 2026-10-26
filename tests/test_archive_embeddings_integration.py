"""Synthetic PostgreSQL+pgvector work, crash recovery and authorization tests."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest

from aster_archive.access import ArchiveScope
from aster_archive.embedding_store import EmbeddingLeaseLost, EmbeddingStore, MAX_ATTEMPTS
from aster_archive.embedding_worker import run_batch
from aster_archive.embeddings import EmbeddedText, LocalE5Encoder, generation_descriptor, generation_id
from aster_archive.ingest import import_path
from aster_archive.storage import ArchiveStorageError
from aster_archive.store import ArchiveNotFound, PostgresArchive


DSN = os.environ.get("ASTER_ARCHIVE_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable PostgreSQL with pgvector; no SQLite fallback")


class SyntheticEncoder:
    def __init__(self, model_version="one"):
        self.descriptor = {**generation_descriptor(), "pipeline": "synthetic-test-" + model_version}
        self.generation_id = generation_id(self.descriptor)
        self.calls = []
        self.query_calls = []
        self.error = None

    def embed_passages(self, texts):
        self.calls.extend(texts)
        if self.error:
            raise self.error
        return [EmbeddedText(([1.0, 0.0] if "semantic" in text else [0.0, 1.0]) + [0.0] * 382,
                             len(text), 1) for text in texts]

    def embed_queries(self, texts):
        self.query_calls.extend(texts)
        return [EmbeddedText([1.0] + [0.0] * 383, len(text), 1) for text in texts]


def chat(identity, text, updated=20):
    return {"id": identity, "title": "Synthetic " + identity, "update_time": updated,
            "current_node": "message", "mapping": {"message": {
                "parent": None, "children": [], "message": {"id": "message", "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [text]}}}}}


@pytest.fixture(scope="module")
def store():
    value = PostgresArchive(DSN)
    value.migrate()
    EmbeddingStore(value).migrate()
    return value


@pytest.fixture
def scope():
    return ArchiveScope("embedding-test-client", "embedding-test-" + uuid.uuid4().hex, ("main",), None)


@pytest.fixture
def encoder():
    return SyntheticEncoder()


def ingest(store, tmp_path, scope, records):
    path = tmp_path / (uuid.uuid4().hex + ".json")
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    result = import_path(store, path, tmp_path / "archive", scope.owner_id, scope.source_ids[0])
    assert result["status"] == "complete"


def ready(store, scope, encoder):
    embeddings = EmbeddingStore(store)
    assert embeddings.register_generation(encoder.descriptor) == encoder.generation_id
    embeddings.enqueue(scope, encoder.generation_id)
    return embeddings


def drain(embeddings, scope, encoder, tmp_path):
    while run_batch(embeddings, scope, encoder, tmp_path / "archive")["completed"]:
        pass
    embeddings.sync_derivations(scope, encoder.generation_id)


def test_additive_migration_preserves_canonical_checksum_and_repeats_safely(store):
    path = Path(__file__).resolve().parents[1] / "aster_archive/schema.sql"
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    EmbeddingStore(store).migrate()
    with store.connection(readonly=True) as conn:
        assert conn.execute("SELECT version,checksum FROM aster_archive.schema_version").fetchall() == [
            {"version": 1, "checksum": expected}]
        assert conn.execute("SELECT version FROM aster_archive.enrichment_migrations WHERE component='embeddings'").fetchall() == [
            {"version": 1}]


def test_hash_dedup_revisions_resume_and_generation_separation(store, scope, encoder, tmp_path):
    text = "semantic Unicode: İstanbul, ışık, şafak ☀"
    ingest(store, tmp_path, scope, [chat("one", text), chat("two", text)])
    embeddings = ready(store, scope, encoder)
    drain(embeddings, scope, encoder, tmp_path)
    assert encoder.calls == [text]
    assert embeddings.sync_derivations(scope, encoder.generation_id) == 0
    historical = store.search(scope, '"İstanbul, ışık"')["results"][0]["id"]
    ingest(store, tmp_path, scope, [chat("one", text, 30), chat("two", text, 30)])
    assert embeddings.enqueue(scope, encoder.generation_id) == 0
    drain(embeddings, scope, encoder, tmp_path)
    assert encoder.calls == [text]
    assert store.fetch(scope, historical)["text"] == text
    coverage = embeddings.coverage(scope, encoder.generation_id)
    assert coverage["current_passages"] == 2 and coverage["unique_texts"] == 1
    assert coverage["embedded_unique_texts"] == 1 and coverage["complete"]
    second = SyntheticEncoder("two")
    ready(store, scope, second)
    drain(embeddings, scope, second, tmp_path)
    assert second.calls == [text]
    with store.connection(readonly=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM aster_archive.embedding_vectors WHERE owner_id=%s",
                            (scope.owner_id,)).fetchone()["n"] == 2
        assert conn.execute("""SELECT DISTINCT d.state,d.pipeline_key FROM aster_archive.derivations d
            JOIN aster_archive.conversations c ON c.current_revision=d.revision_id
            WHERE c.owner_id=%s AND d.kind='embedding'""", (scope.owner_id,)).fetchall() == [
                {"state": "complete", "pipeline_key": second.generation_id}]


def test_semantic_scope_isolation_project_changes_current_revisions_and_tombstones(store, scope, encoder, tmp_path):
    ingest(store, tmp_path, scope, [chat("allowed", "semantic original local archive")])
    hidden_source = replace(scope, source_ids=("hidden-source",))
    ingest(store, tmp_path, hidden_source, [chat("source-secret", "semantic source secret")])
    hidden_owner = replace(scope, owner_id="embedding-other-" + uuid.uuid4().hex)
    ingest(store, tmp_path, hidden_owner, [chat("owner-secret", "semantic owner secret")])
    broad = replace(scope, source_ids=None)
    embeddings = ready(store, broad, encoder)
    drain(embeddings, broad, encoder, tmp_path)
    ready(store, hidden_owner, encoder)
    drain(embeddings, hidden_owner, encoder, tmp_path)
    with store.connection() as conn:
        conn.execute("UPDATE aster_archive.conversations SET project_id='visible' WHERE owner_id=%s AND source_id='main'",
                     (scope.owner_id,))
    scoped = replace(scope, project_ids=("visible",))
    hit = embeddings.search(scoped, "paraphrase", encoder=encoder, mode="semantic")["results"]
    assert len(hit) == 1 and hit[0]["title"] == "Synthetic allowed"
    old_id = hit[0]["id"]
    assert embeddings.coverage(replace(scoped, project_ids=()), encoder.generation_id)["current_passages"] == 0
    assert embeddings.search(replace(scoped, source_ids=()), "query", encoder=encoder)["results"] == []
    ingest(store, tmp_path, scope, [chat("allowed", "replacement current content", 40)])
    embeddings.enqueue(scoped, encoder.generation_id)
    drain(embeddings, scoped, encoder, tmp_path)
    results = embeddings.search(scoped, "paraphrase", encoder=encoder, mode="semantic")["results"]
    assert len(results) == 1 and results[0]["id"] != old_id and results[0]["cosine_similarity"] == 0
    assert store.fetch(scoped, old_id)["text"] == "semantic original local archive"
    with store.connection() as conn:
        conn.execute("UPDATE aster_archive.conversations SET project_id='hidden' WHERE owner_id=%s AND source_id='main'",
                     (scope.owner_id,))
    assert embeddings.search(scoped, "query", encoder=encoder)["results"] == []
    assert embeddings.coverage(scoped, encoder.generation_id)["current_passages"] == 0
    store.tombstone(scope.owner_id, "main", "allowed")
    assert embeddings.search(scope, "query", encoder=encoder)["results"] == []
    with pytest.raises(ArchiveNotFound):
        store.fetch(scope, old_id)


def test_expired_lease_replay_fences_old_worker_without_duplicate_vectors(store, scope, encoder, tmp_path):
    ingest(store, tmp_path, scope, [chat("one", "semantic one"), chat("two", "different two")])
    embeddings = ready(store, scope, encoder)
    old = embeddings.claim(scope, encoder.generation_id)
    assert embeddings.claim(scope, encoder.generation_id) == []
    with store.connection() as conn:
        conn.execute("UPDATE aster_archive.embedding_jobs SET lease_until=now()-interval '1 second' WHERE owner_id=%s",
                     (scope.owner_id,))
    replay = embeddings.claim(scope, encoder.generation_id)
    assert {j["text_sha256"] for j in old} == {j["text_sha256"] for j in replay}
    assert old[0]["lease_token"] != replay[0]["lease_token"]
    values = encoder.embed_passages([job["text"] for job in old])
    with pytest.raises(EmbeddingLeaseLost):
        embeddings.complete(scope, encoder.generation_id, old, values)
    assert embeddings.coverage(scope, encoder.generation_id)["embedded_passages"] == 0
    embeddings.complete(scope, encoder.generation_id, replay,
                        encoder.embed_passages([job["text"] for job in replay]))
    assert embeddings.coverage(scope, encoder.generation_id)["embedded_unique_texts"] == 2
    assert embeddings.claim(scope, encoder.generation_id) == []


def test_vector_publication_and_batch_receipts_share_one_transaction(store, scope, encoder, tmp_path, monkeypatch):
    ingest(store, tmp_path, scope, [chat("one", "semantic atomic one"), chat("two", "atomic two")])
    embeddings = ready(store, scope, encoder)
    jobs = embeddings.claim(scope, encoder.generation_id)
    values = encoder.embed_passages([job["text"] for job in jobs])
    original = store.connection

    class InterruptedConnection:
        def __init__(self, connection):
            self.connection, self.receipts = connection, 0

        def execute(self, query, params=None):
            if "SET state='complete',completed_at" in query:
                self.receipts += 1
                if self.receipts == 2:
                    raise RuntimeError("synthetic interruption before final receipt")
            return self.connection.execute(query, params)

    @contextmanager
    def interrupted(**kwargs):
        with original(**kwargs) as conn:
            yield InterruptedConnection(conn)

    with monkeypatch.context() as patch:
        patch.setattr(store, "connection", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            embeddings.complete(scope, encoder.generation_id, jobs, values)
    coverage = embeddings.coverage(scope, encoder.generation_id)
    assert coverage["embedded_unique_texts"] == 0 and coverage["running_unique_texts"] == 2
    embeddings.complete(scope, encoder.generation_id, jobs, values)
    assert embeddings.coverage(scope, encoder.generation_id)["complete"]


def test_low_space_after_inference_keeps_resumable_unspent_jobs(store, scope, encoder, tmp_path, monkeypatch):
    from aster_archive import embedding_worker
    ingest(store, tmp_path, scope, [chat("one", "semantic resource guard")])
    embeddings = ready(store, scope, encoder)
    checks = []

    def reserve(path):
        checks.append(path)
        if len(checks) == 2:
            raise ArchiveStorageError("synthetic low disk")

    with monkeypatch.context() as patch:
        patch.setattr(embedding_worker, "require_storage_reserve", reserve)
        with pytest.raises(ArchiveStorageError):
            run_batch(embeddings, scope, encoder, tmp_path / "archive")
    coverage = embeddings.coverage(scope, encoder.generation_id)
    assert coverage["pending_unique_texts"] == 1 and coverage["embedded_unique_texts"] == 0
    with store.connection(readonly=True) as conn:
        assert conn.execute("SELECT attempts FROM aster_archive.embedding_jobs WHERE owner_id=%s",
                            (scope.owner_id,)).fetchone()["attempts"] == 0
    drain(embeddings, scope, encoder, tmp_path)
    assert embeddings.coverage(scope, encoder.generation_id)["complete"]


def test_failed_inference_is_sanitized_retryable_and_expired_exhaustion_is_explicit(store, scope, encoder, tmp_path):
    ingest(store, tmp_path, scope, [chat("one", "semantic retry item")])
    embeddings = ready(store, scope, encoder)
    encoder.error = RuntimeError("synthetic model text must not enter receipt")
    with pytest.raises(RuntimeError):
        run_batch(embeddings, scope, encoder, tmp_path / "archive")
    with store.connection() as conn:
        row = conn.execute("SELECT state,error_code FROM aster_archive.embedding_jobs WHERE owner_id=%s",
                           (scope.owner_id,)).fetchone()
        assert row == {"state": "failed", "error_code": "local_inference_failed"}
        conn.execute("UPDATE aster_archive.embedding_jobs SET retry_after=now()-interval '1 second' WHERE owner_id=%s",
                     (scope.owner_id,))
    jobs = embeddings.claim(scope, encoder.generation_id)
    assert len(jobs) == 1
    with store.connection() as conn:
        conn.execute("""UPDATE aster_archive.embedding_jobs SET attempts=%s,lease_until=now()-interval '1 second'
            WHERE owner_id=%s""", (MAX_ATTEMPTS, scope.owner_id))
    assert embeddings.claim(scope, encoder.generation_id) == []
    coverage = embeddings.coverage(scope, encoder.generation_id)
    assert coverage["failed_unique_texts"] == 1 and coverage["running_unique_texts"] == 0
    embeddings.sync_derivations(scope, encoder.generation_id)
    with store.connection(readonly=True) as conn:
        assert conn.execute("""SELECT d.state FROM aster_archive.derivations d
            JOIN aster_archive.conversations c ON c.current_revision=d.revision_id
            WHERE c.owner_id=%s AND d.kind='embedding'""", (scope.owner_id,)).fetchone()["state"] == "failed"
    assert embeddings.retry_failed(scope, encoder.generation_id) == 1
    encoder.error = None
    drain(embeddings, scope, encoder, tmp_path)
    assert embeddings.coverage(scope, encoder.generation_id)["complete"]


def test_hybrid_fuses_lexical_and_semantic_and_quotes_remain_exact(store, scope, encoder, tmp_path):
    ingest(store, tmp_path, scope, [chat("semantic", "semantic meaning only"),
        chat("both", "semantic literal şafak ışığı"), chat("lexical", "literal match only")])
    embeddings = ready(store, scope, encoder)
    drain(embeddings, scope, encoder, tmp_path)
    result = embeddings.search(scope, "literal", encoder=encoder, mode="hybrid")
    assert result["ranking"] == "reciprocal_rank_fusion_k60"
    both = next(row for row in result["results"] if row["title"] == "Synthetic both")
    semantic = next(row for row in result["results"] if row["title"] == "Synthetic semantic")
    assert both["lexical_rank"] is not None and both["semantic_rank"] is not None
    assert both["score"] > semantic["score"]
    calls = len(encoder.query_calls)
    exact = embeddings.search(scope, '"şafak ışığı"', encoder=encoder)
    assert exact["mode"] == "exact" and len(exact["results"]) == 1
    assert len(encoder.query_calls) == calls
    assert store.fetch(scope, exact["results"][0]["id"])["text"] == "semantic literal şafak ışığı"


@pytest.mark.parametrize("foreign_relation", ["owner", "source", "conversation"])
def test_fts_current_pointer_cannot_borrow_foreign_revision(store, scope, encoder, tmp_path,
                                                         foreign_relation):
    ingest(store, tmp_path, scope, [chat("shared", "semantic visible original")])
    foreign = scope
    foreign_chat = "shared"
    if foreign_relation == "owner":
        foreign = replace(scope, owner_id="foreign-owner-" + uuid.uuid4().hex)
    elif foreign_relation == "source":
        foreign = replace(scope, source_ids=("private",))
    else:
        foreign_chat = "hidden-conversation"
    ingest(store, tmp_path, foreign, [chat(foreign_chat, "semantic membershipguard secret")])
    assert len(store.search(foreign, "membershipguard")["results"]) == 1
    broad = replace(scope, source_ids=None)
    embeddings = ready(store, broad, encoder)
    drain(embeddings, broad, encoder, tmp_path)
    if foreign_relation == "owner":
        ready(store, foreign, encoder)
        drain(embeddings, foreign, encoder, tmp_path)
    with store.connection() as conn:
        target = conn.execute("""UPDATE aster_archive.conversations SET project_id='hidden'
            WHERE owner_id=%s AND source_id=%s AND conversation_id=%s
            RETURNING current_revision""",
            (foreign.owner_id, foreign.source_ids[0], foreign_chat)).fetchone()["current_revision"]
        # current_revision has no ownership FK. Search must validate the full
        # revision identity even if an authorized pointer is misbound.
        conn.execute("""UPDATE aster_archive.conversations SET project_id='visible',current_revision=%s
            WHERE owner_id=%s AND source_id=%s AND conversation_id='shared'""",
            (target, scope.owner_id, scope.source_ids[0]))
    scoped = replace(scope, project_ids=("visible",))
    assert store.search(scoped, "membershipguard")["results"] == []
    assert embeddings.search(scoped, "membershipguard", encoder=encoder, mode="hybrid")["results"] == []


def test_fts_limit_follows_scope_with_unembedded_authorized_result(store, scope, encoder, tmp_path):
    ingest(store, tmp_path, scope, [chat("allowed", "limitguard")])
    hidden = replace(scope, source_ids=("private",))
    # More forbidden matches than the hybrid candidate budget (50 at limit=1),
    # each with a higher text rank than the authorized passage.
    ingest(store, tmp_path, hidden, [chat(f"blocked-{i}", "limitguard " * 10) for i in range(55)])
    with store.connection() as conn:
        conn.execute("UPDATE aster_archive.conversations SET project_id='visible' WHERE owner_id=%s",
                     (scope.owner_id,))
    scoped = replace(scope, project_ids=("visible",))
    assert store.search(replace(scoped, source_ids=None), "limitguard", limit=1)["results"][0]["title"].startswith(
        "Synthetic blocked-")
    canonical = store.search(scoped, "limitguard", limit=1)["results"]
    assert len(canonical) == 1 and canonical[0]["title"] == "Synthetic allowed"
    embeddings = ready(store, scoped, encoder)
    # Leave vectors pending, so a semantic result cannot conceal lost lexical
    # candidates when a global text-match limit is applied before access checks.
    hybrid = embeddings.search(scoped, "limitguard", encoder=encoder, mode="hybrid", limit=1)["results"]
    assert [row["id"] for row in hybrid] == [canonical[0]["id"]]
    assert hybrid[0]["lexical_rank"] == 1 and hybrid[0]["semantic_rank"] is None


def test_fts_tie_orders_remain_distinct_for_canonical_and_hybrid(store, scope, encoder, tmp_path):
    ingest(store, tmp_path, scope, [chat(f"tie-{i}", "tieguard") for i in range(4)])
    with store.connection() as conn:
        rows = conn.execute("""SELECT p.passage_id,p.revision_id
            FROM aster_archive.passages p JOIN aster_archive.conversation_revisions r USING(revision_id)
            WHERE r.owner_id=%s ORDER BY p.passage_id""", (scope.owner_id,)).fetchall()
        # Set synthetic sort metadata after IDs are known: date and ID orders
        # must differ, with both a date tie and a NULL timestamp represented.
        for row, updated in zip(rows, (10, None, 10, 20), strict=True):
            conn.execute("UPDATE aster_archive.conversation_revisions SET source_updated=%s WHERE revision_id=%s",
                         (updated, row["revision_id"]))
    identities = [row["passage_id"] for row in rows]
    canonical = store.search(scope, "tieguard")["results"]
    assert [row["id"] for row in canonical] == [identities[3], identities[0], identities[2], identities[1]]
    embeddings = ready(store, scope, encoder)
    hybrid = embeddings.search(scope, "tieguard", encoder=encoder, mode="hybrid")["results"]
    assert [row["id"] for row in hybrid] == identities
    assert [row["lexical_rank"] for row in hybrid] == [1, 2, 3, 4]


@pytest.mark.skipif(not os.environ.get("ASTER_ARCHIVE_TEST_MODEL_DIR"),
                    reason="requires prepared local E5 model; never downloads in tests")
def test_real_e5_cross_language_semantic_hit_round_trips_exact_unicode(store, scope, tmp_path):
    encoder = LocalE5Encoder(Path(os.environ["ASTER_ARCHIVE_TEST_MODEL_DIR"]))
    original = "Özel arşiv yedeklerini yerel bilgisayarımızda saklıyoruz."
    ingest(store, tmp_path, scope, [chat("backup", original),
        chat("recipe", "Domatesleri zeytinyağı ve tuzla birlikte pişirin."),
        chat("sport", "Futbol maçı bu akşam stadyumda oynanacak.")])
    embeddings = ready(store, scope, encoder)
    drain(embeddings, scope, encoder, tmp_path)
    query = "Where do we keep private archive backup copies?"
    assert store.search(scope, query)["results"] == []
    result = embeddings.search(scope, query, encoder=encoder, mode="semantic")
    assert result["coverage"]["complete"]
    assert result["results"][0]["title"] == "Synthetic backup"
    fetched = store.fetch(scope, result["results"][0]["id"])
    assert fetched["text"] == original
    assert fetched["metadata"]["text_sha256"] == hashlib.sha256(original.encode()).hexdigest()
