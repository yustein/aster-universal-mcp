"""One real reader probe against a tiny, isolated synthetic enriched archive."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from aster_archive.access import ArchiveScope
from aster_archive.embedding_store import EmbeddingStore
from aster_archive.embedding_worker import run_batch
from aster_archive.embeddings import LocalE5Encoder
from aster_archive.ingest import import_path
from aster_archive.knowledge import KnowledgeStore, derive_pending
from aster_archive.store import PostgresArchive

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("enrichment_recovery_probe", ROOT / "deploy/archive/recovery_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_enrichment_is_optional_without_touching_database():
    scope = ArchiveScope("synthetic", "synthetic-owner", ("synthetic-source",), None)
    assert probe.enrichment_probe(None, scope, SimpleNamespace(source="synthetic-source")) == {}


def test_real_enrichment_reader_returns_only_metadata_and_rejects_missing_pilot(tmp_path, monkeypatch):
    dsn = os.environ.get("ASTER_ARCHIVE_RECOVERY_TEST_DSN")
    model = os.environ.get("ASTER_ARCHIVE_RECOVERY_MODEL_DIR")
    if not dsn or not model:
        pytest.skip("requires disposable PostgreSQL with pgvector and a verified local E5 model")
    token = uuid.uuid4().hex[:16]
    database, reader_role = "aster_recovery_probe_" + token, "aster_probe_read_" + token
    values = conninfo_to_dict(dsn)
    owner, source = "synthetic-recovery-" + token, "synthetic-source"
    scope = ArchiveScope("synthetic-reader", owner, (source,), None)
    args = SimpleNamespace(source=source, query="archived project decision", check_embedding=True,
                           check_knowledge=True, check_cognee=True)
    monkeypatch.setenv("ASTER_ARCHIVE_EMBEDDING_MODEL_DIR", model)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(reader_role)))
        created = False
        try:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
            created = True
            admin_dsn = make_conninfo(**(values | {"dbname": database}))
            store = PostgresArchive(admin_dsn)
            with store.connection() as db:
                db.execute("CREATE EXTENSION vector WITH SCHEMA public")
            store.migrate()
            index = EmbeddingStore(store)
            index.migrate()
            KnowledgeStore(store).migrate()
            text = "I decided to keep the synthetic recovery archive. İstanbul source quotation survives."
            path = tmp_path / "synthetic.json"
            path.write_text(json.dumps([{"id": "synthetic-conversation", "title": "PRIVATE SYNTHETIC TITLE",
                "current_node": "one", "mapping": {"one": {"parent": None, "children": [],
                "message": {"id": "one", "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": [text]}}}}}]))
            root = tmp_path / "archive"
            assert import_path(store, path, root, owner, source)["status"] == "complete"
            encoder = LocalE5Encoder(Path(model), threads=1, batch_size=1)
            index.register_generation(encoder.descriptor)
            index.enqueue(scope, encoder.generation_id)
            assert run_batch(index, scope, encoder, root)["completed"] == 1
            assert derive_pending(store, owner, source, root)["completed"] == 1
            del encoder
            with store.connection() as db:
                db.execute(sql.SQL("GRANT USAGE ON SCHEMA aster_archive TO {}").format(sql.Identifier(reader_role)))
                db.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA aster_archive TO {}").format(sql.Identifier(reader_role)))
            admin.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(sql.Identifier(reader_role)))
            reader = PostgresArchive(make_conninfo(**(values | {"dbname": database, "user": reader_role})))
            # A running Cognee container without a completed durable pilot must
            # not produce a ready checkpoint. No model-heavy Cognee run is needed.
            with pytest.raises(AssertionError):
                probe.enrichment_probe(reader, scope, SimpleNamespace(source=source, check_cognee=True))
            from aster_archive.cognee_bridge import PIPELINE_KEY
            with store.connection() as db:
                db.execute("""INSERT INTO aster_archive.cognee_jobs(passage_id,pipeline_key,status,receipt)
                    SELECT passage_id,%s,'complete','{"summary_count":0}'::jsonb FROM aster_archive.passages""",
                    (PIPELINE_KEY,))
            result = probe.enrichment_probe(reader, scope, args)
            assert result["embedding"]["semantic_results"] == 1
            assert result["embedding"]["cross_owner_denied"] is True
            assert result["knowledge"]["exact_citation_valid"] is True
            assert result["knowledge"]["cross_owner_denied"] is True
            assert result["cognee"]["completed_passages"] == 1
            assert result["cognee"]["derived_summaries"] == 0
            serialized = json.dumps(result)
            assert text not in serialized and "PRIVATE SYNTHETIC TITLE" not in serialized
            with reader.connection(readonly=True) as db:
                assert db.execute("SHOW default_transaction_read_only").fetchone()["default_transaction_read_only"] == "on"
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    db.execute("DELETE FROM aster_archive.passages")
        finally:
            if created:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(reader_role)))
