"""PostgreSQL canonical archive and scoped, read-only retrieval.

Every conversation revision, its passages, downstream obligations and import
checkpoint share one transaction. Immutable originals live outside PostgreSQL.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
from importlib import resources
import json
import re
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import PIPELINE_VERSION
from .access import ArchiveScope
from .source import SourceArchive

PASSAGE_CHARS = 2000
PASSAGE_STRIDE = 1000
MAX_SEARCH_CHARS = 1000
MAX_ANCESTORS = 2048
MAX_CONTEXT_BYTES = 48_000
_HEX = re.compile(r"[0-9a-f]{64}\Z")


class ArchiveNotFound(LookupError):
    pass


class ArchiveBusy(RuntimeError):
    pass


def stable_id(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise ValueError("invalid archive identity")
    return value


def _scope_sql(scope: ArchiveScope, alias: str = "c") -> tuple[str, list]:
    _identity(scope.owner_id)
    terms = [f"{alias}.owner_id=%s", f"{alias}.tombstoned_at IS NULL"]
    values: list = [scope.owner_id]
    if scope.source_ids is not None:
        terms.append(f"{alias}.source_id=ANY(%s::text[])")
        values.append(list(scope.source_ids))
    if scope.project_ids is not None:
        terms.append(f"{alias}.project_id=ANY(%s::text[])")
        values.append(list(scope.project_ids))
    return " AND ".join(terms), values


def _fts_search_sql(scope: ArchiveScope, query: str, limit: int, *,
                    order_by_updated: bool) -> tuple[str, list]:
    """Plan text matching before correlated membership joins, without a hit cap.

    Materializing only passage-local matches lets PostgreSQL use the text
    indexes instead of scanning every passage of each authorized revision.
    Scope and full revision ownership are still checked before the final
    ordering and limit; current_revision alone is not an ownership guarantee.
    """
    where, args = _scope_sql(scope)
    updated_order = "r.source_updated DESC NULLS LAST," if order_by_updated else ""
    sql = f"""WITH q AS (SELECT plainto_tsquery('simple',%s) s,
            plainto_tsquery('english',%s) e,plainto_tsquery('turkish',%s) t),
        hits AS MATERIALIZED (
            SELECT p.passage_id,p.revision_id,
                greatest(ts_rank_cd(p.lexemes_simple,q.s),ts_rank_cd(p.lexemes_english,q.e),
                         ts_rank_cd(p.lexemes_turkish,q.t)) AS score
            FROM aster_archive.passages p,q
            WHERE p.lexemes_simple@@q.s OR p.lexemes_english@@q.e OR p.lexemes_turkish@@q.t)
        SELECT h.passage_id,r.title,h.score
        FROM hits h JOIN aster_archive.conversation_revisions r USING(revision_id)
        WHERE EXISTS (SELECT 1 FROM aster_archive.conversations c
            WHERE {where} AND c.owner_id=r.owner_id AND c.source_id=r.source_id
              AND c.conversation_id=r.conversation_id AND c.current_revision=r.revision_id)
        ORDER BY h.score DESC,{updated_order}h.passage_id LIMIT %s"""
    return sql, [query] * 3 + args + [limit]


_PASSAGE_SELECT = """
    SELECT p.passage_id,p.revision_id,p.node_id,p.part_index,p.start_char,
           p.end_char,p.text,p.text_sha256,n.role,n.parent_id,
           r.owner_id,r.source_id,r.conversation_id,r.title,r.source_created,
           r.source_updated,r.current_node,c.project_id,c.current_revision,
           c.current_ambiguous
      FROM aster_archive.passages p
      JOIN aster_archive.nodes n USING (revision_id,node_id)
      JOIN aster_archive.conversation_revisions r USING (revision_id)
      JOIN aster_archive.conversations c USING (owner_id,source_id,conversation_id)
"""


def _passage_result(row: dict) -> dict:
    keys = ("source_id", "conversation_id", "revision_id", "node_id", "role",
            "parent_id", "part_index", "start_char", "end_char", "text_sha256",
            "source_created", "source_updated", "current_node", "project_id")
    return {
        "id": row["passage_id"],
        "title": row["title"] or "Untitled conversation",
        "text": row["text"],
        "metadata": {
            **{key: row[key] for key in keys},
            "is_current_revision": row["revision_id"] == row["current_revision"],
            "current_revision_ambiguous": row["current_ambiguous"],
            "offset_unit": "Unicode characters in the original content part",
            "passage_window_chars": PASSAGE_CHARS,
            "passage_stride_chars": PASSAGE_STRIDE,
            "overlap_with_previous_chars": (
                min(PASSAGE_CHARS - PASSAGE_STRIDE, len(row["text"]))
                if row["start_char"] > 0 else 0
            ),
            "reconstruction": "Merge passages by content part and source offsets; neighboring windows overlap.",
            "source_type": "chatgpt_export",
            "content_is_historical_evidence": True,
        },
    }


class PostgresArchive:
    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn:
            raise ValueError("archive database configuration required")
        self.dsn = dsn

    @contextmanager
    def connection(self, *, readonly: bool = False) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, connect_timeout=10, row_factory=dict_row) as conn:
            if readonly:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                conn.execute("SET LOCAL statement_timeout='15s'")
            yield conn

    def migrate(self) -> None:
        ddl = resources.files("aster_archive").joinpath("schema.sql").read_text()
        checksum = hashlib.sha256(ddl.encode()).hexdigest()
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (717697834001,))
            exists = conn.execute("SELECT to_regclass('aster_archive.schema_version') AS relation").fetchone()
            if exists["relation"]:
                rows = conn.execute("SELECT version,checksum FROM aster_archive.schema_version").fetchall()
                if rows != [{"version": 1, "checksum": checksum}]:
                    raise RuntimeError("archive schema version or checksum mismatch")
                return
            conn.execute(ddl)
            conn.execute("INSERT INTO aster_archive.schema_version(version,checksum) VALUES (1,%s)", (checksum,))

    def register_import(self, source: SourceArchive, owner_id: str, source_id: str) -> str:
        _identity(owner_id)
        _identity(source_id)
        import_id = stable_id(owner_id, source_id, source.sha256, PIPELINE_VERSION)
        with self.connection() as conn:
            conn.execute("INSERT INTO aster_archive.sources(owner_id,source_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                         (owner_id, source_id))
            conn.execute("""INSERT INTO aster_archive.imports
                (import_id,owner_id,source_id,archive_sha256,object_path,size_bytes,kind,pipeline_version,inventory)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                         (import_id, owner_id, source_id, source.sha256, str(source.path),
                          source.size, source.kind, PIPELINE_VERSION, Jsonb(list(source.inventory))))
            conn.execute("INSERT INTO aster_archive.import_outbox(import_id) VALUES (%s) ON CONFLICT DO NOTHING",
                         (import_id,))
        return import_id

    def get_import(self, import_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM aster_archive.imports WHERE import_id=%s", (import_id,)).fetchone()
            if row is None:
                raise ArchiveNotFound("archive import unavailable")
            return row

    @contextmanager
    def import_lock(self, owner_id: str, source_id: str) -> Iterator[psycopg.Connection]:
        lock_id = int.from_bytes(bytes.fromhex(stable_id(owner_id, source_id))[:8], "big", signed=True)
        # A connection-scoped lock serializes pointer updates across imports of
        # one account. All writes use this same connection; connection loss
        # aborts processing rather than reconnecting under a stale lock.
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10, row_factory=dict_row) as conn:
            locked = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (lock_id,)).fetchone()["locked"]
            if not locked:
                raise ArchiveBusy("another import is running for this source")
            try:
                yield conn
            finally:
                if not conn.closed:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))

    def item_exists(self, conn, import_id: str, member: str, ordinal: int) -> bool:
        return conn.execute("""SELECT 1 FROM aster_archive.import_items
            WHERE import_id=%s AND member_name=%s AND ordinal=%s""",
                            (import_id, member, ordinal)).fetchone() is not None

    def commit_item(self, conn, import_id: str, member: str, ordinal: int, item: dict) -> None:
        with conn.transaction():
            imp = conn.execute("SELECT owner_id,source_id FROM aster_archive.imports WHERE import_id=%s",
                               (import_id,)).fetchone()
            owner, source, cid = imp["owner_id"], imp["source_id"], item["conversation_id"]
            revision = stable_id(owner, source, cid, item["content_hash"])
            conn.execute("""INSERT INTO aster_archive.conversations(owner_id,source_id,conversation_id)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", (owner, source, cid))
            current = conn.execute("""SELECT * FROM aster_archive.conversations
                WHERE owner_id=%s AND source_id=%s AND conversation_id=%s FOR UPDATE""", (owner, source, cid)).fetchone()
            if current["tombstoned_at"] is not None:
                conn.execute("""INSERT INTO aster_archive.import_items
                    (import_id,member_name,ordinal,conversation_id,status)
                    VALUES (%s,%s,%s,%s,'tombstoned') ON CONFLICT DO NOTHING""", (import_id, member, ordinal, cid))
                return
            inserted = conn.execute("""INSERT INTO aster_archive.conversation_revisions
                (revision_id,owner_id,source_id,conversation_id,content_hash,title,source_created,
                 source_updated,current_node,metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING revision_id""",
                                    (revision, owner, source, cid, item["content_hash"], item["title"],
                                     item["created_at"], item["updated_at"], item["current_node"],
                                     Jsonb(item["metadata"]))).fetchone()
            if inserted:
                node_rows, passage_rows = [], []
                for node in item["nodes"]:
                    node_rows.append((revision, node["node_id"], node["parent_id"], Jsonb(node["children"]),
                                      node["message_id"], node["role"], node["content_type"], node["content_hash"],
                                      node["searchable"], Jsonb(node["raw"]), Jsonb(node.get("asset_refs", []))))
                    if not node["searchable"]:
                        continue
                    for part in node.get("text_parts", []):
                        # Each passage contains only original text, with no
                        # synthetic separators or prefixes. Offsets are local
                        # to one original content part.
                        # The overlap makes any supported whole quoted query
                        # discoverable even when it crosses a window boundary.
                        # Exact source offsets disclose and resolve duplication.
                        for start in range(0, len(part["text"]), PASSAGE_STRIDE):
                            text = part["text"][start:start + PASSAGE_CHARS]
                            pid = stable_id(revision, node["node_id"], part["part_index"], start)
                            passage_rows.append((pid, revision, node["node_id"], part["part_index"], start,
                                                 start + len(text), text, hashlib.sha256(text.encode()).hexdigest()))
                            if start + len(text) == len(part["text"]):
                                break
                with conn.cursor() as cursor:
                    cursor.executemany("""INSERT INTO aster_archive.nodes
                        (revision_id,node_id,parent_id,children,message_id,role,content_type,content_hash,searchable,raw,asset_refs)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", node_rows)
                    cursor.executemany("""INSERT INTO aster_archive.passages
                        (passage_id,revision_id,node_id,part_index,start_char,end_char,text,text_sha256)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", passage_rows)
                conn.execute("""INSERT INTO aster_archive.derivations(revision_id,kind)
                    VALUES (%s,'embedding'),(%s,'knowledge')""", (revision, revision))
            incoming = item["updated_at"]
            previous = current["current_updated"]
            if current["current_revision"] is None or (
                incoming is not None and previous is not None and incoming > previous
            ):
                # Advancing past the selected dated revision does not resolve
                # the chronology of a different retained undated revision.
                # Equal-time alternatives also remain explicitly ambiguous.
                ambiguous = conn.execute("""SELECT EXISTS (
                    SELECT 1 FROM aster_archive.conversation_revisions
                    WHERE owner_id=%s AND source_id=%s AND conversation_id=%s
                      AND revision_id<>%s AND (source_updated IS NULL OR source_updated=%s)
                    ) AS ambiguous""", (owner, source, cid, revision, incoming)).fetchone()["ambiguous"]
                conn.execute("""UPDATE aster_archive.conversations SET current_revision=%s,
                    current_updated=%s,current_ambiguous=%s WHERE owner_id=%s AND source_id=%s AND conversation_id=%s""",
                             (revision, incoming, ambiguous, owner, source, cid))
            elif current["current_revision"] != revision and (
                incoming is None or previous is None or incoming == previous
            ):
                # Import order cannot establish source chronology. Retain both
                # revisions, leave the pointer, and surface the ambiguity.
                conn.execute("""UPDATE aster_archive.conversations SET current_ambiguous=true
                    WHERE owner_id=%s AND source_id=%s AND conversation_id=%s""", (owner, source, cid))
            conn.execute("""INSERT INTO aster_archive.import_items
                (import_id,member_name,ordinal,conversation_id,revision_id,status)
                VALUES (%s,%s,%s,%s,%s,'imported') ON CONFLICT DO NOTHING""", (import_id, member, ordinal, cid, revision))

    def commit_invalid(self, conn, import_id: str, member: str, ordinal: int, code: str) -> None:
        with conn.transaction():
            conn.execute("""INSERT INTO aster_archive.import_items(import_id,member_name,ordinal,status,error_code)
                VALUES (%s,%s,%s,'invalid',%s) ON CONFLICT DO NOTHING""", (import_id, member, ordinal, code))

    def import_report(self, import_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            imp = conn.execute("""SELECT import_id,owner_id,source_id,archive_sha256,size_bytes,status,
                created_at,started_at,finished_at,error_code FROM aster_archive.imports WHERE import_id=%s""",
                               (import_id,)).fetchone()
            if imp is None:
                raise ArchiveNotFound("archive import unavailable")
            counts = conn.execute("""SELECT status,count(*) AS count FROM aster_archive.import_items
                WHERE import_id=%s GROUP BY status""", (import_id,)).fetchall()
            return {**imp, "records": {row["status"]: row["count"] for row in counts}}

    def search(self, scope: ArchiveScope, query: str, limit: int = 20) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_CHARS or "\x00" in query:
            raise ValueError("invalid search query")
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("invalid search limit")
        query = query.strip()
        exact = query[1:-1] if len(query) >= 2 and query.startswith('"') and query.endswith('"') else None
        if exact == "":
            raise ValueError("empty exact phrase")
        if exact is not None:
            # Exact phrase mode also handles punctuation-only terms. It is
            # bounded by the read transaction's statement timeout.
            where, args = _scope_sql(scope)
            sql = f"""SELECT p.passage_id,r.title,n.role,1.0 AS score
                FROM aster_archive.passages p JOIN aster_archive.conversation_revisions r USING(revision_id)
                JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
                JOIN aster_archive.nodes n USING(revision_id,node_id)
                WHERE {where} AND c.current_revision=p.revision_id AND strpos(p.text,%s)>0
                ORDER BY score DESC,r.source_updated DESC NULLS LAST,p.passage_id LIMIT %s"""
            parameters = args + [exact, limit]
        else:
            sql, parameters = _fts_search_sql(scope, query, limit, order_by_updated=True)
        with self.connection(readonly=True) as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return {"results": [{"id": row["passage_id"], "title": row["title"] or "Untitled conversation"}
                            for row in rows]}

    def _scoped_passage(self, conn, scope: ArchiveScope, passage_id: str) -> dict:
        if not isinstance(passage_id, str) or not _HEX.fullmatch(passage_id):
            raise ValueError("invalid passage identity")
        where, values = _scope_sql(scope)
        row = conn.execute(_PASSAGE_SELECT + f" WHERE {where} AND p.passage_id=%s", values + [passage_id]).fetchone()
        if row is None:
            raise ArchiveNotFound("archive passage unavailable")
        return row

    def fetch(self, scope: ArchiveScope, passage_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            return _passage_result(self._scoped_passage(conn, scope, passage_id))

    def context(self, scope: ArchiveScope, passage_id: str, limit: int = 20, cursor: str | None = None) -> dict:
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("invalid context limit")
        scope_key = stable_id(scope.owner_id, scope.source_ids, scope.project_ids)
        offset = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(cursor))
                if data["v"] != 1 or data["id"] != passage_id or data["scope"] != scope_key:
                    raise ValueError()
                offset = data["offset"]
                if type(offset) is not int or not 0 <= offset <= 1_000_000:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ValueError("invalid context cursor") from exc
        with self.connection(readonly=True) as conn:
            target = self._scoped_passage(conn, scope, passage_id)
            scope_where, scope_args = _scope_sql(scope)
            ancestry = conn.execute("""WITH RECURSIVE a AS (
                SELECT node_id,parent_id,0 AS depth,ARRAY[node_id] AS path,false AS cycle
                FROM aster_archive.nodes WHERE revision_id=%s AND node_id=%s
                UNION ALL
                SELECT n.node_id,n.parent_id,a.depth+1,a.path||n.node_id,n.node_id=ANY(a.path)
                FROM a JOIN aster_archive.nodes n ON n.node_id=a.parent_id AND n.revision_id=%s
                WHERE a.depth<%s AND NOT a.cycle)
                SELECT node_id,parent_id,depth,cycle FROM a ORDER BY depth DESC""",
                                    (target["revision_id"], target["node_id"], target["revision_id"], MAX_ANCESTORS)).fetchall()
            nodes = [row["node_id"] for row in ancestry if not row["cycle"]]
            status = "complete"
            if any(row["cycle"] for row in ancestry):
                status = "cycle_in_source"
            elif ancestry and ancestry[0]["parent_id"] is not None:
                status = "ancestor_limit" if ancestry[0]["depth"] >= MAX_ANCESTORS else "missing_parent_in_source"
            rows = conn.execute(_PASSAGE_SELECT + f"""
                WHERE {scope_where} AND p.revision_id=%s AND p.node_id=ANY(%s::text[])
                  AND (p.node_id<>%s OR p.part_index<%s OR (p.part_index=%s AND p.start_char<=%s))
                ORDER BY array_position(%s::text[],p.node_id),p.part_index,p.start_char
                LIMIT %s OFFSET %s""",
                                scope_args + [target["revision_id"], nodes, target["node_id"], target["part_index"],
                                 target["part_index"], target["start_char"], nodes, limit + 1, offset]).fetchall()
            passages, used = [], 0
            for row in rows[:limit]:
                result = _passage_result(row)
                size = len(json.dumps(result, ensure_ascii=False).encode())
                if passages and used + size > MAX_CONTEXT_BYTES:
                    break
                passages.append(result)
                used += size
            more = len(rows) > len(passages)
            next_cursor = None
            if more:
                payload = {"v": 1, "id": passage_id, "scope": scope_key, "offset": offset + len(passages)}
                next_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            return {"passages": passages, "next_cursor": next_cursor, "ancestry_status": status,
                    "direction": "ancestors_to_selected_passage", "revision_id": target["revision_id"]}

    def coverage(self, scope: ArchiveScope) -> dict:
        where, args = _scope_sql(scope)
        with self.connection(readonly=True) as conn:
            counts = conn.execute(f"""SELECT count(*) AS conversations,
                count(*) FILTER (WHERE current_ambiguous) AS ambiguous_current_revisions
                FROM aster_archive.conversations c WHERE {where}""", args).fetchone()
            facts = conn.execute(f"""SELECT count(*) AS revisions,min(r.source_created) AS earliest_source_creation,
                max(r.source_updated) AS latest_source_update
                FROM aster_archive.conversation_revisions r JOIN aster_archive.conversations c
                USING(owner_id,source_id,conversation_id) WHERE {where}""", args).fetchone()
            passages = conn.execute(f"""SELECT count(*) AS exact_passages
                FROM aster_archive.passages p JOIN aster_archive.conversation_revisions r USING(revision_id)
                JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id) WHERE {where}""", args).fetchone()
            imported = None
            if scope.project_ids is None:
                params = [scope.owner_id]
                extra = ""
                if scope.source_ids is not None:
                    extra = " AND i.source_id=ANY(%s::text[])"
                    params.append(list(scope.source_ids))
                imported = conn.execute(f"""SELECT status,count(*) AS snapshots,max(created_at) AS last_observed_at
                    FROM aster_archive.imports i WHERE owner_id=%s {extra} GROUP BY status""", params).fetchall()
                for entry in imported:
                    entry["last_observed_at"] = entry["last_observed_at"].isoformat()
        return {**counts, **facts, **passages, "imports": imported,
                "available_account_total": None, "complete_account_history_verified": False,
                "coverage_basis": "successfully parsed records in supplied snapshots",
                "asset_status": "original ZIP retained; referenced assets not yet independently extracted or verified",
                "embeddings": "held", "knowledge_extraction": "held"}

    def status(self, owner_id: str, source_id: str) -> dict:
        scope = ArchiveScope(client_id="local-operator", owner_id=_identity(owner_id),
                             source_ids=(_identity(source_id),), project_ids=None)
        result = self.coverage(scope)
        with self.connection(readonly=True) as conn:
            result["imports_detail"] = conn.execute("""SELECT import_id,archive_sha256,status,created_at,
                started_at,finished_at,error_code FROM aster_archive.imports
                WHERE owner_id=%s AND source_id=%s ORDER BY created_at DESC LIMIT 50""", (owner_id, source_id)).fetchall()
        return result

    def tombstone(self, owner_id: str, source_id: str, conversation_id: str) -> bool:
        with self.import_lock(owner_id, source_id) as conn:
            with conn.transaction():
                conn.execute("INSERT INTO aster_archive.sources(owner_id,source_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                             (_identity(owner_id), _identity(source_id)))
                conn.execute("""INSERT INTO aster_archive.conversations(owner_id,source_id,conversation_id,tombstoned_at)
                    VALUES (%s,%s,%s,now()) ON CONFLICT(owner_id,source_id,conversation_id)
                    DO UPDATE SET tombstoned_at=COALESCE(aster_archive.conversations.tombstoned_at,now())""",
                             (owner_id, source_id, _identity(conversation_id)))
        return True

    def inbox_seen(self, owner_id: str, source_id: str, path: str, size: int, mtime_ns: int) -> bool:
        with self.connection(readonly=True) as conn:
            return conn.execute("""SELECT 1 FROM aster_archive.inbox_receipts WHERE owner_id=%s AND source_id=%s
                AND path=%s AND size_bytes=%s AND mtime_ns=%s""", (owner_id, source_id, path, size, mtime_ns)).fetchone() is not None

    def record_inbox(self, owner_id: str, source_id: str, path: str, size: int, mtime_ns: int,
                     import_id: str | None = None, error_code: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute("""INSERT INTO aster_archive.inbox_receipts
                (owner_id,source_id,path,size_bytes,mtime_ns,import_id,error_code) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""", (owner_id, source_id, path, size, mtime_ns, import_id, error_code))
