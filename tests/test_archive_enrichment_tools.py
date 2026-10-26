"""Authenticated enrichment tools, explicit fallback and verified citations."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from aster_archive.access import ArchiveScope
from aster_archive.tools import ArchiveReader, ArchiveRequestError, register_archive_tools

from test_archive_tools import (
    ORIGIN, PID, ScopedStore, caller, grant, invoke, write_acl,
)


KID = "d" * 64
RID = "c" * 64
GENERATION = "f" * 64
TEXT = "I approve using PostgreSQL. İstanbul için yerel yedek."


class EvidenceStore(ScopedStore):
    def fetch(self, scope, passage_id):
        result = super().fetch(scope, passage_id)
        return {**result, "text": TEXT, "metadata": {
            **result["metadata"], "revision_id": RID, "conversation_id": "conversation",
            "node_id": "message", "part_index": 0, "start_char": 0, "end_char": len(TEXT),
            "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
        }}


def record():
    end = TEXT.index(".") + 1
    return {
        "id": KID, "kind": "decision_candidate", "title": "Synthetic candidate",
        "text": TEXT[:end], "metadata": {
            "revision_id": RID, "source_id": "personal", "conversation_id": "conversation",
            "pipeline_key": "synthetic-extractor", "verified_decision": False,
        }, "citations": [{
            "passage_id": PID, "revision_id": RID, "node_id": "message",
            "source_id": "personal", "conversation_id": "conversation",
            "role": "user", "part_index": 0, "start_char": 0, "end_char": end,
            "passage_start_char": 0, "passage_end_char": len(TEXT), "exact_quote": TEXT[:end],
        }],
    }


class SemanticBackend:
    def __init__(self, store):
        self.store, self.calls, self.error = store, [], None

    def search(self, scope, query, *, encoder, mode="hybrid", limit=20):
        self.calls.append(("search", scope, query, mode, limit))
        if self.error:
            raise self.error
        return {"results": [{"id": PID, "title": "Aster", "score": 0.9}]
                if self.store._allowed(scope) else [],
                "mode": mode, "generation_id": encoder.generation_id,
                "coverage": self.coverage(scope, encoder.generation_id)}

    def coverage(self, scope, generation_id=None):
        self.calls.append(("coverage", scope, generation_id))
        total = int(self.store._allowed(scope))
        return {"generation_id": generation_id, "current_passages": total,
                "embedded_passages": 0, "complete": total == 0}


class KnowledgeBackend:
    def __init__(self, store):
        self.store, self.calls = store, []
        self.value = record()

    def search(self, scope, query, kind=None, limit=20):
        self.calls.append(("search", scope, query, kind, limit))
        return {"results": [copy.deepcopy(self.value)] if self.store._allowed(scope) else [],
                "basis": "derived_records_current_revisions"}

    def fetch(self, scope, identity):
        self.calls.append(("fetch", scope, identity))
        if not self.store._allowed(scope) or identity != KID:
            raise LookupError("not available")
        return copy.deepcopy(self.value)

    def history(self, scope, conversation_id, limit=20, cursor=None):
        self.calls.append(("history", scope, conversation_id, limit, cursor))
        return {"records": [copy.deepcopy(self.value)] if self.store._allowed(scope) else [],
                "next_cursor": None, "scope": "retained revisions; candidates are not verified decisions"}

    def coverage(self, scope):
        self.calls.append(("coverage", scope))
        return {"records": [{"kind": "decision_candidate", "records": 1}] if self.store._allowed(scope) else [],
                "jobs": [], "verified_decisions": 0}


@pytest.fixture
def enriched(tmp_path):
    acl = tmp_path / "grants.json"
    write_acl(acl, {"owner-client": grant(),
                    "worker": grant(sources=["work"], projects=["fleet"])})
    store = EvidenceStore()
    embeddings, knowledge = SemanticBackend(store), KnowledgeBackend(store)
    encoder = SimpleNamespace(generation_id=GENERATION)
    reader = ArchiveReader(store, origin=ORIGIN, grants_path=acl,
                           embeddings=embeddings, encoder=encoder, knowledge=knowledge,
                           cognee_coverage=lambda scope: {"jobs": [], "graph_is_not_authoritative": True})
    server = MCPServer("enriched archive test")
    register_archive_tools(server, reader)
    return server, reader, store, embeddings, knowledge, acl


def test_default_hybrid_partial_coverage_and_explicit_modes(enriched):
    server, reader, store, embeddings, knowledge, acl = enriched
    with caller():
        result = invoke(server, "search", {"query": "Aster"})
        assert result["mode"] == "hybrid" and result["partial_embedding_coverage"] is True
        assert result["results"][0]["url"] == reader.url(PID)
        semantic = invoke(server, "archive_search", {"query": "Aster", "mode": "semantic", "limit": 1})
        assert semantic["mode"] == "semantic" and semantic["partial_embedding_coverage"]
        before = len(embeddings.calls)
        lexical = invoke(server, "archive_search", {"query": "Aster", "mode": "lexical"})
        assert lexical["mode"] == "lexical" and len(embeddings.calls) == before
        coverage = invoke(server, "archive_coverage")
        assert coverage["embeddings"]["query_model_ready"] is True
        assert coverage["embeddings"]["index"]["embedded_passages"] == 0
        assert coverage["knowledge_extraction"]["state"] == "available"
        assert coverage["knowledge_extraction"]["verified_decisions"] == 0
        assert coverage["cognee"]["graph_is_not_authoritative"] is True
    assert all(call[1] == ArchiveScope("owner-client", "owner", None, None) for call in embeddings.calls)


def test_fallback_is_explicit_sanitized_and_semantic_never_silently_falls_back(enriched):
    server, reader, store, embeddings, knowledge, acl = enriched
    embeddings.error = RuntimeError("postgresql://private:secret@private-host")
    with caller():
        result = invoke(server, "search", {"query": "Aster"})
        assert result["mode"] == "lexical" and result["requested_mode"] == "hybrid"
        assert result["fallback"] == {"reason": "semantic_unavailable", "semantic_results_included": False}
        assert "private-host" not in json.dumps(result) and "secret" not in json.dumps(result)
        with pytest.raises(ToolError, match="SEMANTIC_UNAVAILABLE"):
            invoke(server, "archive_search", {"query": "Aster", "mode": "semantic"})
        before = len(embeddings.calls)
        exact = invoke(server, "archive_search", {"query": '"İstanbul"', "mode": "semantic"})
        assert exact["mode"] == "exact" and len(embeddings.calls) == before
        reader.encoder = None
        reader.embedding_status = "model_unavailable"
        degraded = invoke(server, "search", {"query": "Aster"})
        assert degraded["fallback"]["reason"] == "model_unavailable"


def test_knowledge_sources_have_canonical_protected_urls_and_verified_exact_spans(enriched):
    server, reader, store, embeddings, knowledge, acl = enriched
    knowledge.value["citations"][0]["url"] = "https://untrusted.invalid/fabricated"
    knowledge.value["url"] = "https://untrusted.invalid/derived"
    with caller():
        search = invoke(server, "archive_knowledge_search", {"query": "PostgreSQL", "kind": "decision_candidate"})
        fetched = invoke(server, "archive_knowledge_fetch", {"id": KID})
        history = invoke(server, "archive_decision_history", {"conversation_id": "conversation", "limit": 1})
    for value in (search["results"][0], fetched, history["records"][0]):
        assert value["citations"][0]["url"] == reader.url(PID)
        assert value["citations"][0]["exact_quote"] == TEXT[:TEXT.index(".")+1]
        assert value["metadata"]["citation_spans_verified"] is True
        assert value["metadata"]["verified_decision"] is False
        assert "url" not in value
    assert all(call[1] == ArchiveScope("owner-client", "owner", None, None) for call in knowledge.calls)
    assert len([call for call in store.calls if call[0] == "fetch"]) == 3


@pytest.mark.parametrize("tool,arguments", [
    ("archive_search", {"query": "Aster", "mode": "semantic"}),
    ("archive_knowledge_search", {"query": "PostgreSQL"}),
    ("archive_knowledge_fetch", {"id": KID}),
    ("archive_decision_history", {"conversation_id": "conversation"}),
])
def test_new_tools_authenticate_before_backend_and_reload_revoked_grants(enriched, tool, arguments):
    server, reader, store, embeddings, knowledge, acl = enriched
    with pytest.raises(ToolError, match="AUTHENTICATION_REQUIRED"):
        invoke(server, tool, arguments)
    assert store.calls == embeddings.calls == knowledge.calls == []
    with caller():
        invoke(server, tool, arguments)
        before = (len(store.calls), len(embeddings.calls), len(knowledge.calls))
        write_acl(acl, {})
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            invoke(server, tool, arguments)
        assert before == (len(store.calls), len(embeddings.calls), len(knowledge.calls))


def test_denied_scope_never_gets_semantic_or_knowledge_content(enriched, monkeypatch):
    server, reader, store, embeddings, knowledge, acl = enriched
    with caller("worker"):
        assert invoke(server, "archive_search", {"query": "Aster", "mode": "semantic"})["results"] == []
        assert invoke(server, "archive_knowledge_search", {"query": "PostgreSQL"})["results"] == []
        assert invoke(server, "archive_decision_history", {"conversation_id": "conversation"})["records"] == []
        with pytest.raises(ToolError, match="PASSAGE_NOT_FOUND"):
            invoke(server, "archive_knowledge_fetch", {"id": KID})
        # A stale/corrupt derived citation still cannot override canonical scope.
        monkeypatch.setattr(knowledge, "fetch", lambda *args: record())
        with pytest.raises(ToolError, match="PASSAGE_NOT_FOUND"):
            invoke(server, "archive_knowledge_fetch", {"id": KID})


@pytest.mark.parametrize("mutation", [
    lambda row: row["citations"][0].update(exact_quote="Invented approval"),
    lambda row: row["citations"][0].update(start_char=-1),
    lambda row: row["citations"][0].update(revision_id="e" * 64),
    lambda row: row.update(citations=row["citations"] * 33),
])
def test_bad_citations_fail_closed(enriched, mutation):
    server, reader, store, embeddings, knowledge, acl = enriched
    mutation(knowledge.value)
    with caller(), pytest.raises(ToolError, match="RESPONSE_UNAVAILABLE"):
        invoke(server, "archive_knowledge_fetch", {"id": KID})


def test_knowledge_response_limits_reject_before_canonical_fetch(enriched):
    server, reader, store, embeddings, knowledge, acl = enriched
    knowledge.value["text"] = "x" * (1024 * 1024)
    with caller(), pytest.raises(ToolError, match="RESPONSE_LIMIT"):
        invoke(server, "archive_knowledge_fetch", {"id": KID})
    assert store.calls == []


def test_enrichment_request_bounds_are_checked_before_backend(enriched):
    server, reader, store, embeddings, knowledge, acl = enriched
    with caller():
        for method, args in (
            (reader.search_mode, {"query": "x", "mode": "other"}),
            (reader.search_mode, {"query": "x", "limit": True}),
            (reader.knowledge_search, {"query": "x", "kind": "accepted"}),
            (reader.knowledge_fetch, {"record_id": "../private"}),
            (reader.decision_history, {"conversation_id": "x", "cursor": "x" * 1025}),
            (reader.decision_history, {"conversation_id": "x", "limit": 21}),
        ):
            with pytest.raises(ArchiveRequestError, match="INVALID_"):
                method(**args)
    assert store.calls == embeddings.calls == knowledge.calls == []


def test_reader_model_cache_is_singleton_and_fixed_cpu_budget(tmp_path, monkeypatch):
    import aster_archive.embeddings as models
    import aster_archive.service as service

    monkeypatch.setattr(service, "_ENCODER", None)
    monkeypatch.setattr(service, "_ENCODER_PATH", None)
    calls = []
    def construct(path, **kwargs):
        calls.append((path, kwargs))
        return object()
    monkeypatch.setattr(models, "LocalE5Encoder", construct)
    first = service._local_encoder(str(tmp_path))
    assert service._local_encoder(str(tmp_path)) is first
    assert calls == [(tmp_path, {"threads": 2, "batch_size": 4})]
    another = tmp_path / "another"
    another.mkdir()
    with pytest.raises(ValueError, match="restart"):
        service._local_encoder(str(another))


def test_service_model_failure_has_read_only_fallback_and_citations_keep_bearer_acl(tmp_path, monkeypatch):
    import aster_archive.service as service
    from aster_archive.embedding_store import EmbeddingStore
    from aster_archive.knowledge import KnowledgeStore
    from client_registry import ClientRegistry
    from token_store import TokenStore

    def forbidden(*args, **kwargs):
        raise AssertionError("reader must never migrate or download")
    monkeypatch.setattr(EmbeddingStore, "migrate", forbidden)
    monkeypatch.setattr(KnowledgeStore, "migrate", forbidden)
    def broken_model(path):
        raise RuntimeError("private model path and internal diagnostic")
    monkeypatch.setattr(service, "_local_encoder", broken_model)
    database, acl = tmp_path / "oauth.sqlite", tmp_path / "grants.json"
    write_acl(acl, {})
    store = EvidenceStore()
    app = service.create_app(backend=store, embedding_backend=SemanticBackend(store),
        knowledge_backend=KnowledgeBackend(store), cognee_coverage=lambda scope: {"jobs": []},
        environment={"ASTER_ARCHIVE_PUBLIC_ORIGIN": ORIGIN,
            "ASTER_ARCHIVE_OAUTH_DB": str(database), "ASTER_ARCHIVE_CLIENTS": str(acl),
            "ASTER_ARCHIVE_ENABLE_ENRICHMENT": "1",
            "ASTER_ARCHIVE_EMBEDDING_MODEL_DIR": str(tmp_path / "unavailable-model")})
    client_id = ClientRegistry(str(database)).register("Synthetic archive", ["https://example.invalid/callback"])
    token = TokenStore(str(database)).issue(client_id=client_id, resource=ORIGIN + "/mcp", scope="system:admin")
    wrong = TokenStore(str(database)).issue(client_id=client_id, resource="https://wrong.invalid/mcp", scope="system:admin")
    write_acl(acl, {client_id: grant()})

    async def exercise():
        inner = app.app
        headers = {"Host": "archive-test.invalid", "X-Forwarded-Proto": "https",
                   "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token}
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 10000)),
                                         base_url=ORIGIN, headers=headers) as http:
                async def tool(name, arguments):
                    response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})
                    assert response.status_code == 200
                    result = response.json()["result"]
                    assert not result.get("isError", False), result
                    return result.get("structuredContent") or json.loads(result["content"][0]["text"])
                result = await tool("search", {"query": "Aster"})
                assert result["fallback"]["reason"] == "model_unavailable"
                assert "private model path" not in json.dumps(result)
                coverage = await tool("archive_coverage", {})
                assert coverage["embeddings"]["reader_state"] == "model_unavailable"
                derived = await tool("archive_knowledge_fetch", {"id": KID})
                url = derived["citations"][0]["url"]
                assert (await http.get(url, headers={"Authorization": ""})).status_code == 401
                assert (await http.get(url, headers={"Authorization": "Bearer " + wrong})).status_code == 401
                assert (await http.get(url)).json()["text"] == TEXT
                write_acl(acl, {})
                assert (await http.get(url)).status_code == 403
    asyncio.run(asyncio.wait_for(exercise(), 15))


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_main_migration_enrichment_is_explicit_and_ordered(monkeypatch, capsys, enabled):
    from aster_archive import __main__ as command
    from aster_archive import queue
    from aster_archive.embedding_store import EmbeddingStore
    from aster_archive.knowledge import KnowledgeStore

    calls = []
    class Store:
        def __init__(self, dsn):
            pass
        def migrate(self):
            calls.append("base")
    monkeypatch.setenv("ASTER_ARCHIVE_DATABASE_URL", "synthetic-dsn")
    monkeypatch.setenv("ASTER_ARCHIVE_ENABLE_ENRICHMENT", enabled)
    monkeypatch.setattr(command, "PostgresArchive", Store)
    monkeypatch.setattr(queue, "migrate_queue", lambda dsn: calls.append("queue"))
    monkeypatch.setattr(EmbeddingStore, "migrate", lambda self: calls.append("embeddings"))
    monkeypatch.setattr(KnowledgeStore, "migrate", lambda self: calls.append("knowledge"))
    assert command.main(["migrate"]) == 0
    assert calls == ["base", "queue"] + (["embeddings", "knowledge"] if enabled == "1" else [])
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "schemas_ready"


@pytest.mark.skipif(not os.environ.get("ASTER_ARCHIVE_TEST_DSN"), reason="requires real disposable PostgreSQL+pgvector")
def test_actual_postgres_embedding_and_knowledge_backends_through_mcp(tmp_path):
    from aster_archive.embedding_store import EmbeddingStore
    from aster_archive.embedding_worker import run_batch
    from aster_archive.ingest import import_path
    from aster_archive.knowledge import KnowledgeStore, derive_pending
    from aster_archive.store import PostgresArchive
    from test_archive_embeddings_integration import SyntheticEncoder, chat

    store = PostgresArchive(os.environ["ASTER_ARCHIVE_TEST_DSN"])
    embeddings, knowledge = EmbeddingStore(store), KnowledgeStore(store)
    embeddings.migrate()
    knowledge.migrate()
    owner = "mcp-enrichment-test-" + uuid.uuid4().hex
    scope = ArchiveScope("owner-client", owner, ("personal",), None)
    source = tmp_path / "source.json"
    source.write_text(json.dumps([chat("conversation", "I approve using PostgreSQL for our semantic archive.")] ))
    assert import_path(store, source, tmp_path / "archive", owner, "personal")["status"] == "complete"
    encoder = SyntheticEncoder()
    embeddings.register_generation(encoder.descriptor)
    embeddings.enqueue(scope, encoder.generation_id)
    assert run_batch(embeddings, scope, encoder, tmp_path / "archive")["completed"] == 1
    assert derive_pending(store, owner, "personal", tmp_path / "archive")["completed"] == 1
    acl = tmp_path / "grants.json"
    write_acl(acl, {"owner-client": grant(owner=owner, sources=["personal"])})
    reader = ArchiveReader(store, origin=ORIGIN, grants_path=acl, embeddings=embeddings,
                           encoder=encoder, knowledge=knowledge)
    server = MCPServer("actual enriched database")
    register_archive_tools(server, reader)
    with caller():
        found = invoke(server, "archive_search", {"query": "database selection", "mode": "semantic"})
        assert found["coverage"]["complete"] and found["results"]
        exact = invoke(server, "search", {"query": '"I approve using PostgreSQL"'})
        assert exact["mode"] == "exact" and len(exact["results"]) == 1
        result = invoke(server, "archive_knowledge_search", {"query": "PostgreSQL", "kind": "decision_candidate"})
        assert len(result["results"]) == 1
        value = result["results"][0]
        fetched = invoke(server, "archive_knowledge_fetch", {"id": value["id"]})
        assert fetched["metadata"]["citation_spans_verified"]
        assert fetched["citations"][0]["url"] == reader.url(found["results"][0]["id"])
        history = invoke(server, "archive_decision_history", {"conversation_id": "conversation"})
        assert history["records"][0]["id"] == value["id"]
        store.tombstone(owner, "personal", "conversation")
        assert invoke(server, "archive_search", {"query": "database selection", "mode": "semantic"})["results"] == []
        assert invoke(server, "archive_knowledge_search", {"query": "PostgreSQL"})["results"] == []
        with pytest.raises(ToolError, match="PASSAGE_NOT_FOUND"):
            invoke(server, "archive_knowledge_fetch", {"id": value["id"]})
