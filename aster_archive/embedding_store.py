"""Durable owner-deduplicated embedding work and scoped exact-vector retrieval."""
from __future__ import annotations

import hashlib
import re
import uuid

from psycopg.types.json import Jsonb

from .embeddings import DIMENSIONS, EmbeddedText, generation_id, validate_vector
from .enrichment import migrate_component
from .store import MAX_SEARCH_CHARS, _scope_sql


_HASH = re.compile(r"[a-f0-9]{64}\Z")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 300
RRF_K = 60


class EmbeddingLeaseLost(RuntimeError):
    pass


class EmbeddingUnavailable(RuntimeError):
    pass


def _hash(value: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("invalid embedding identity")
    return value


def _limit(value: int, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid embedding limit")
    return value


def _vector(value) -> str:
    return "[" + ",".join(format(item, ".9g") for item in validate_vector(value)) + "]"


_CURRENT = """FROM aster_archive.passages p
    JOIN aster_archive.conversation_revisions r USING(revision_id)
    JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)"""


class EmbeddingStore:
    def __init__(self, store):
        self.store = store

    def migrate(self) -> None:
        migrate_component(self.store, "embeddings", 1, "embeddings_schema.sql")

    def register_generation(self, descriptor: dict) -> str:
        identity = generation_id(descriptor)
        with self.store.connection() as conn:
            conn.execute("""INSERT INTO aster_archive.embedding_generations
                (generation_id,descriptor,dimensions) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                         (identity, Jsonb(descriptor), DIMENSIONS))
            saved = conn.execute("""SELECT descriptor FROM aster_archive.embedding_generations
                WHERE generation_id=%s""", (identity,)).fetchone()
            if saved["descriptor"] != descriptor:
                raise RuntimeError("embedding generation identity mismatch")
        return identity

    def enqueue(self, scope, identity: str, *, limit: int = 2000) -> int:
        """Discover new hashes from authorized current passages before inference."""
        _hash(identity)
        _limit(limit, 10000)
        where, args = _scope_sql(scope)
        with self.store.connection() as conn:
            result = conn.execute(f"""INSERT INTO aster_archive.embedding_jobs
                (generation_id,owner_id,text_sha256)
                SELECT %s,%s,missing.text_sha256 FROM (
                    SELECT DISTINCT p.text_sha256 {_CURRENT}
                    WHERE {where} AND c.current_revision=p.revision_id
                      AND NOT EXISTS (SELECT 1 FROM aster_archive.embedding_jobs j
                          WHERE j.generation_id=%s AND j.owner_id=c.owner_id
                            AND j.text_sha256=p.text_sha256)
                    ORDER BY p.text_sha256 LIMIT %s
                ) missing ON CONFLICT DO NOTHING""",
                [identity, scope.owner_id] + args + [identity, limit])
            return result.rowcount

    def claim(self, scope, identity: str, *, limit: int = 4,
              lease_seconds: int = LEASE_SECONDS) -> list[dict]:
        """Claim a bounded batch; an expired lease can safely be claimed again."""
        _hash(identity)
        _limit(limit, 16)
        _limit(lease_seconds, 3600)
        where, args = _scope_sql(scope)
        token = uuid.uuid4().hex
        with self.store.connection() as conn:
            conn.execute(f"""UPDATE aster_archive.embedding_jobs j SET state='failed',
                error_code='lease_attempts_exhausted',lease_token=NULL,lease_until=NULL,retry_after=NULL
                WHERE j.generation_id=%s AND j.owner_id=%s AND j.state='running'
                  AND j.lease_until<=now() AND j.attempts>=%s
                  AND EXISTS (SELECT 1 {_CURRENT} WHERE {where}
                      AND c.current_revision=p.revision_id AND p.text_sha256=j.text_sha256)""",
                [identity, scope.owner_id, MAX_ATTEMPTS] + args)
            rows = conn.execute(f"""WITH candidate AS (
                SELECT j.generation_id,j.owner_id,j.text_sha256
                FROM aster_archive.embedding_jobs j
                WHERE j.generation_id=%s AND j.owner_id=%s AND j.attempts<%s
                  AND j.state IN ('pending','running','failed')
                  AND (j.state='pending' OR (j.state='failed' AND j.retry_after<=now())
                       OR (j.state='running' AND j.lease_until<=now()))
                  AND EXISTS (SELECT 1 {_CURRENT} WHERE {where}
                      AND c.current_revision=p.revision_id AND p.text_sha256=j.text_sha256)
                ORDER BY j.created_at,j.text_sha256 LIMIT %s FOR UPDATE OF j SKIP LOCKED
            ) UPDATE aster_archive.embedding_jobs j SET state='running',attempts=j.attempts+1,
                lease_token=%s,lease_until=now()+(%s*interval '1 second'),retry_after=NULL,error_code=NULL
              FROM candidate q WHERE (j.generation_id,j.owner_id,j.text_sha256)=
                                    (q.generation_id,q.owner_id,q.text_sha256)
              RETURNING j.text_sha256,j.lease_token,j.attempts""",
                [identity, scope.owner_id, MAX_ATTEMPTS] + args + [limit, token, lease_seconds]).fetchall()
            result = []
            for row in rows:
                passage = conn.execute(f"""SELECT p.text {_CURRENT} WHERE {where}
                    AND c.current_revision=p.revision_id AND p.text_sha256=%s
                    ORDER BY p.passage_id LIMIT 1""", args + [row["text_sha256"]]).fetchone()
                # Both statements share this transaction. A concurrent import
                # may change the pointer; release that job instead of using an
                # arbitrary historical or out-of-scope representative.
                if passage is None:
                    conn.execute("""UPDATE aster_archive.embedding_jobs SET state='pending',
                        attempts=attempts-1,lease_token=NULL,lease_until=NULL
                        WHERE generation_id=%s AND owner_id=%s AND text_sha256=%s AND lease_token=%s""",
                        (identity, scope.owner_id, row["text_sha256"], token))
                    continue
                if hashlib.sha256(passage["text"].encode()).hexdigest() != row["text_sha256"]:
                    raise RuntimeError("canonical passage hash mismatch")
                result.append({**row, "text": passage["text"]})
            return result

    def complete(self, scope, identity: str, jobs: list[dict], values: list[EmbeddedText]) -> int:
        """Publish vectors and checkpoints atomically, fencing stale workers."""
        _hash(identity)
        if not jobs or len(jobs) != len(values) or len(jobs) > 16:
            raise ValueError("invalid embedding result batch")
        prepared = []
        for job, value in zip(jobs, values, strict=True):
            _hash(job["text_sha256"])
            if (type(value.token_count) is not int or not 0 <= value.token_count <= 100000
                    or type(value.window_count) is not int or not 1 <= value.window_count <= 64):
                raise ValueError("invalid embedding input metadata")
            prepared.append((job, value, _vector(value.vector)))
        prepared.sort(key=lambda item: item[0]["text_sha256"])
        with self.store.connection() as conn:
            for job, value, vector in prepared:
                row = conn.execute("""SELECT state,lease_token FROM aster_archive.embedding_jobs
                    WHERE generation_id=%s AND owner_id=%s AND text_sha256=%s FOR UPDATE""",
                    (identity, scope.owner_id, job["text_sha256"])).fetchone()
                if row is None or row["state"] != "running" or row["lease_token"] != job["lease_token"]:
                    raise EmbeddingLeaseLost("ARCHIVE_EMBEDDING_LEASE_LOST")
                conn.execute("""INSERT INTO aster_archive.embedding_vectors
                    (generation_id,owner_id,text_sha256,embedding,token_count,window_count)
                    VALUES (%s,%s,%s,%s::public.vector,%s,%s) ON CONFLICT DO NOTHING""",
                    (identity, scope.owner_id, job["text_sha256"], vector,
                     value.token_count, value.window_count))
                conn.execute("""UPDATE aster_archive.embedding_jobs SET state='complete',completed_at=now(),
                    lease_token=NULL,lease_until=NULL,retry_after=NULL,error_code=NULL
                    WHERE generation_id=%s AND owner_id=%s AND text_sha256=%s""",
                    (identity, scope.owner_id, job["text_sha256"]))
        return len(jobs)

    def release(self, scope, identity: str, jobs: list[dict]) -> None:
        """Release a resource-paused batch without spending its retry budget."""
        _hash(identity)
        with self.store.connection() as conn:
            for job in jobs:
                conn.execute("""UPDATE aster_archive.embedding_jobs SET state='pending',
                    attempts=greatest(0,attempts-1),lease_token=NULL,lease_until=NULL,
                    retry_after=NULL,error_code=NULL
                    WHERE generation_id=%s AND owner_id=%s AND text_sha256=%s
                      AND state='running' AND lease_token=%s""",
                    (identity, scope.owner_id, _hash(job["text_sha256"]), job["lease_token"]))

    def fail(self, scope, identity: str, jobs: list[dict], *, code: str = "local_inference_failed") -> None:
        _hash(identity)
        if code not in {"local_inference_failed", "storage_unavailable", "worker_interrupted"}:
            raise ValueError("invalid embedding error code")
        with self.store.connection() as conn:
            for job in jobs:
                conn.execute("""UPDATE aster_archive.embedding_jobs SET state='failed',error_code=%s,
                    retry_after=now()+(least(300,30*attempts)*interval '1 second'),
                    lease_token=NULL,lease_until=NULL
                    WHERE generation_id=%s AND owner_id=%s AND text_sha256=%s
                      AND state='running' AND lease_token=%s""",
                    (code, identity, scope.owner_id, _hash(job["text_sha256"]), job["lease_token"]))

    def retry_failed(self, scope, identity: str) -> int:
        """Explicit local operator retry; never exposed as an MCP mutation."""
        _hash(identity)
        where, args = _scope_sql(scope)
        with self.store.connection() as conn:
            return conn.execute(f"""UPDATE aster_archive.embedding_jobs j SET state='pending',attempts=0,
                retry_after=NULL,error_code=NULL,lease_token=NULL,lease_until=NULL
                WHERE j.generation_id=%s AND j.owner_id=%s AND j.state='failed'
                  AND EXISTS (SELECT 1 {_CURRENT} WHERE {where} AND c.current_revision=p.revision_id
                              AND p.text_sha256=j.text_sha256)""",
                [identity, scope.owner_id] + args).rowcount

    def sync_derivations(self, scope, identity: str) -> int:
        """Reflect completeness only after all current revision hashes exist."""
        _hash(identity)
        where, args = _scope_sql(scope)
        with self.store.connection() as conn:
            return conn.execute(f"""WITH desired AS (
                SELECT c.current_revision AS revision_id,
                CASE WHEN EXISTS (SELECT 1 FROM aster_archive.passages p
                    JOIN aster_archive.embedding_jobs j ON j.text_sha256=p.text_sha256
                      AND j.owner_id=c.owner_id AND j.generation_id=%s
                    WHERE p.revision_id=c.current_revision AND j.state='failed' AND j.attempts>=%s)
                    THEN 'failed'
                WHEN EXISTS (SELECT 1 FROM aster_archive.passages p
                    WHERE p.revision_id=c.current_revision AND NOT EXISTS (
                        SELECT 1 FROM aster_archive.embedding_vectors v WHERE v.generation_id=%s
                          AND v.owner_id=c.owner_id AND v.text_sha256=p.text_sha256))
                    THEN 'pending' ELSE 'complete' END AS state
                FROM aster_archive.conversations c WHERE {where})
                UPDATE aster_archive.derivations d SET pipeline_key=%s,state=desired.state
                FROM desired WHERE d.revision_id=desired.revision_id AND d.kind='embedding'
                  AND (d.pipeline_key IS DISTINCT FROM %s OR d.state IS DISTINCT FROM desired.state)""",
                [identity, MAX_ATTEMPTS, identity] + args + [identity, identity]).rowcount

    def _coverage(self, conn, scope, identity: str) -> dict:
        where, args = _scope_sql(scope)
        counts = conn.execute(f"""WITH eligible AS MATERIALIZED (
            SELECT p.text_sha256 {_CURRENT} WHERE {where} AND c.current_revision=p.revision_id)
            SELECT count(*) AS current_passages,count(v.text_sha256) AS embedded_passages,
                   count(DISTINCT p.text_sha256) AS unique_texts,
                   count(DISTINCT v.text_sha256) AS embedded_unique_texts
            FROM eligible p LEFT JOIN aster_archive.embedding_vectors v
              ON v.text_sha256=p.text_sha256 AND v.owner_id=%s AND v.generation_id=%s""",
            args + [scope.owner_id, identity]).fetchone()
        states = conn.execute(f"""WITH eligible AS MATERIALIZED (
            SELECT DISTINCT p.text_sha256 {_CURRENT} WHERE {where} AND c.current_revision=p.revision_id)
            SELECT j.state,count(*) AS count FROM eligible p LEFT JOIN aster_archive.embedding_jobs j
              ON j.text_sha256=p.text_sha256 AND j.owner_id=%s AND j.generation_id=%s
            GROUP BY j.state""", args + [scope.owner_id, identity]).fetchall()
        by_state = {row["state"]: row["count"] for row in states}
        return {"generation_id": identity, **counts,
                "pending_unique_texts": by_state.get("pending", 0),
                "running_unique_texts": by_state.get("running", 0),
                "failed_unique_texts": by_state.get("failed", 0),
                "unseeded_unique_texts": by_state.get(None, 0),
                "complete": counts["current_passages"] == counts["embedded_passages"],
                "basis": "authorized current revisions; tombstoned conversations excluded"}

    def coverage(self, scope, generation_id: str | None = None) -> dict:
        with self.store.connection(readonly=True) as conn:
            if generation_id is None:
                where, args = _scope_sql(scope)
                rows = conn.execute(f"""SELECT DISTINCT j.generation_id {_CURRENT}
                    JOIN aster_archive.embedding_jobs j ON j.owner_id=c.owner_id AND j.text_sha256=p.text_sha256
                    WHERE {where} AND c.current_revision=p.revision_id ORDER BY j.generation_id""", args).fetchall()
                return {"generations": [self._coverage(conn, scope, row["generation_id"]) for row in rows]}
            return self._coverage(conn, scope, _hash(generation_id))

    def search(self, scope, query: str, *, encoder, mode: str = "hybrid", limit: int = 20) -> dict:
        if (not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_CHARS
                or "\x00" in query or mode not in {"semantic", "hybrid"}):
            raise ValueError("invalid semantic search")
        _limit(limit, 50)
        query = query.strip()
        identity = _hash(encoder.generation_id)
        if identity != generation_id(encoder.descriptor):
            raise ValueError("embedding generation mismatch")
        if query.startswith('"') and query.endswith('"') and len(query) >= 2:
            # Quoted literals retain the exact existing search contract. No
            # semantic candidate can satisfy an exact phrase by paraphrase.
            return {**self.store.search(scope, query, limit=limit), "mode": "exact",
                    "generation_id": identity}
        embedded = encoder.embed_queries([query])
        if len(embedded) != 1:
            raise ValueError("invalid query embedding result")
        vector = _vector(embedded[0].vector)
        where, args = _scope_sql(scope)
        candidates = min(200, max(50, limit * 4))
        with self.store.connection(readonly=True) as conn:
            saved = conn.execute("""SELECT descriptor FROM aster_archive.embedding_generations
                WHERE generation_id=%s""", (identity,)).fetchone()
            if saved is None or saved["descriptor"] != encoder.descriptor:
                raise EmbeddingUnavailable("ARCHIVE_EMBEDDING_GENERATION_UNAVAILABLE")
            # Scope/current/tombstone checks happen in the vector query before
            # ranking. No global nearest-neighbor candidate cache is trusted.
            semantic = conn.execute(f"""SELECT p.passage_id,r.title,
                    1-(v.embedding <=> %s::public.vector) AS score
                {_CURRENT} JOIN aster_archive.embedding_vectors v
                  ON v.text_sha256=p.text_sha256 AND v.owner_id=c.owner_id AND v.generation_id=%s
                WHERE {where} AND c.current_revision=p.revision_id
                ORDER BY v.embedding <=> %s::public.vector,p.passage_id LIMIT %s""",
                [vector, identity] + args + [vector, candidates]).fetchall()
            lexical = []
            if mode == "hybrid":
                lexical = conn.execute(f"""WITH q AS (SELECT plainto_tsquery('simple',%s) s,
                    plainto_tsquery('english',%s) e,plainto_tsquery('turkish',%s) t)
                    SELECT p.passage_id,r.title,greatest(ts_rank_cd(p.lexemes_simple,q.s),
                        ts_rank_cd(p.lexemes_english,q.e),ts_rank_cd(p.lexemes_turkish,q.t)) AS score
                    {_CURRENT},q WHERE {where} AND c.current_revision=p.revision_id
                      AND (p.lexemes_simple@@q.s OR p.lexemes_english@@q.e OR p.lexemes_turkish@@q.t)
                    ORDER BY score DESC,p.passage_id LIMIT %s""", [query] * 3 + args + [candidates]).fetchall()
            coverage = self._coverage(conn, scope, identity)
        results = {}
        for kind, rows in (("semantic", semantic), ("lexical", lexical)):
            for rank, row in enumerate(rows, start=1):
                hit = results.setdefault(row["passage_id"], {
                    "id": row["passage_id"], "title": row["title"] or "Untitled conversation",
                    "score": 0.0, "semantic_rank": None, "lexical_rank": None,
                })
                hit[kind + "_rank"] = rank
                if kind == "semantic":
                    hit["cosine_similarity"] = float(row["score"])
                hit["score"] += 1.0 / (RRF_K + rank) if mode == "hybrid" else float(row["score"])
        ordered = sorted(results.values(), key=lambda row: (-row["score"], row["id"]))[:limit]
        return {"results": ordered, "mode": mode, "generation_id": identity, "coverage": coverage,
                "ranking": "reciprocal_rank_fusion_k60" if mode == "hybrid" else "exact_cosine",
                "scores_are_probabilities": False,
                "content_is_historical_evidence": True}
