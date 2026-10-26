"""Private, metadata-only recovery evidence, run in the existing archive image.

No server is started. Database access always uses the service's reader DSN.
The extract mode runs only in a separate container with new disposable volumes.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import uuid


DEFAULT_MAX_MEMBERS = 2_000_000
MAX_MEMBER_BUDGET = 50_000_000
MIN_FREE_INODES = 10_000
MAX_MEMBER_NAME_BYTES = 4096
MAX_MEMBER_DEPTH = 64
STORAGE_RESERVE = 10 * 1024**3


def validate_member_budget(value):
    if type(value) is not int or not 1 <= value <= MAX_MEMBER_BUDGET:
        raise ValueError("restore member budget must be between 1 and 50000000")
    return value


def require_inode_reserve(root, needed):
    capacity = os.statvfs(root)
    if capacity.f_files <= 0 or capacity.f_favail < MIN_FREE_INODES + needed:
        raise ValueError("restore inode reserve reached or unavailable")


def missing_directories(directory, root):
    missing = []
    while directory != root and not directory.exists():
        missing.append(directory)
        directory = directory.parent
    if not directory.is_dir():
        raise ValueError("backup parent is not a directory")
    return list(reversed(missing))


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract_stream(max_bytes, base=Path("/restore"), *, max_members=DEFAULT_MAX_MEMBERS):
    validate_member_budget(max_members)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("positive restore byte budget required")
    roots = {name: base / name for name in ("archive", "auth", "config")}
    if not all(root.is_dir() and not any(root.iterdir()) for root in roots.values()):
        raise ValueError("restore volumes must be empty")
    require_inode_reserve(roots["archive"], 2)  # Private index directory and database.
    if shutil.disk_usage(roots["archive"]).free < STORAGE_RESERVE + 1024**2:
        raise ValueError("restore storage reserve reached")
    total, files, members = 0, 0, 0
    # A Python set of names and TarFile's default header cache both grow with
    # every member. Keep duplicate detection on the disposable volume instead;
    # this private temporary directory is never accepted from the archive.
    with tempfile.TemporaryDirectory(prefix=".aster-restore-index-", dir=roots["archive"]) as temporary:
        index_root = Path(temporary)
        with closing(sqlite3.connect(index_root / "members.sqlite3", isolation_level=None)) as seen:
            seen.execute("PRAGMA journal_mode=OFF")
            seen.execute("PRAGMA synchronous=OFF")
            seen.execute("PRAGMA cache_size=-8192")
            seen.execute("PRAGMA temp_store=MEMORY")
            seen.execute("CREATE TABLE members(name TEXT PRIMARY KEY) WITHOUT ROWID")
            with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
                while (member := archive.next()) is not None:
                    # Python 3.12 retains TarInfo even in stream mode. Links are
                    # forbidden and extractfile receives this exact TarInfo, so
                    # no previous header is needed to extract the current one.
                    archive.members.clear()
                    members += 1
                    if members > max_members:
                        raise ValueError("restore member budget exceeded")
                    path = PurePosixPath(member.name)
                    if (path.is_absolute() or "\\" in member.name or "\x00" in member.name
                            or ".." in path.parts or not path.parts or path.parts[0] not in roots
                            or len(member.name.encode("utf-8")) > MAX_MEMBER_NAME_BYTES
                            or len(path.parts) > MAX_MEMBER_DEPTH
                            or not (member.isdir() or member.isfile())):
                        raise ValueError("unsupported backup member")
                    name = path.as_posix()
                    if path.parts[0] != "archive" and name not in (
                            "auth", "config", "auth/oauth.sqlite3", "config/clients.json"):
                        raise ValueError("unsupported private-state member")
                    if len(path.parts) > 1 and path.parts[:2] == ("archive", index_root.name):
                        raise ValueError("reserved restore index member")
                    # Account for index growth even for a stream of directories
                    # or empty files. This remains a headroom check, not a quota.
                    if shutil.disk_usage(roots["archive"]).free < STORAGE_RESERVE + 64 * 1024:
                        raise ValueError("restore storage reserve reached")
                    try:
                        seen.execute("INSERT INTO members(name) VALUES (?)", (name,))
                    except sqlite3.IntegrityError:
                        raise ValueError("duplicate backup member") from None
                    destination = base / path
                    root = roots[path.parts[0]]
                    directories = missing_directories(destination if member.isdir() else destination.parent, root)
                    require_inode_reserve(root, len(directories) + int(member.isfile()))
                    if member.isfile():
                        total += member.size
                        if member.size < 0 or total > max_bytes:
                            raise ValueError("restore byte budget exceeded")
                    if shutil.disk_usage(root).free < STORAGE_RESERVE + (member.size if member.isfile() else 0):
                        raise ValueError("restore storage reserve reached")
                    for directory in directories:
                        directory.mkdir(mode=0o700)
                        os.chown(directory, 10001, 10001, follow_symlinks=False)
                    if member.isdir():
                        os.chown(destination, 10001, 10001, follow_symlinks=False)
                    else:
                        source = archive.extractfile(member)
                        if source is None:
                            raise ValueError("missing backup member stream")
                        with source, destination.open("xb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        if destination.stat().st_size != member.size:
                            raise ValueError("truncated backup member")
                        destination.chmod(0o600 if member.mode & 0o200 else 0o400)
                        os.chown(destination, 10001, 10001, follow_symlinks=False)
                        files += 1
    for root in roots.values():
        os.chown(root, 10001, 10001, follow_symlinks=False)
    if not (roots["auth"] / "oauth.sqlite3").is_file() or not (roots["config"] / "clients.json").is_file():
        raise ValueError("missing private-state members")
    return {"files": files, "members": members, "max_members": max_members,
            "bytes": total, "separate_volumes": True, "min_free_inodes": MIN_FREE_INODES}


def enrichment_probe(store, scope, args):
    """Bounded read-only evidence; optional indexes may still be filling."""
    from aster_archive.access import ArchiveScope
    from aster_archive.store import ArchiveNotFound, _scope_sql

    result = {}
    denied = ArchiveScope("local-recovery-denied", "denied-" + uuid.uuid4().hex, (args.source,), None)

    def checked_passage(passage_id):
        passage = store.fetch(scope, passage_id)
        metadata, text = passage["metadata"], passage["text"]
        assert metadata["source_id"] == args.source and metadata["is_current_revision"] is True
        assert 0 <= metadata["start_char"] <= metadata["end_char"]
        assert len(text) == metadata["end_char"] - metadata["start_char"] <= 2000
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == metadata["text_sha256"]
        return passage

    if getattr(args, "check_embedding", False):
        from aster_archive.embedding_store import EmbeddingStore
        from aster_archive.embeddings import LocalE5Encoder

        model = Path(os.environ.get("ASTER_ARCHIVE_EMBEDDING_MODEL_DIR",
                                    "/var/lib/aster-archive-models/e5-small"))
        encoder = LocalE5Encoder(model, threads=1, batch_size=1)
        index = EmbeddingStore(store)
        coverage = index.coverage(scope, encoder.generation_id)
        assert coverage["embedded_passages"] > 0
        query = args.query.strip().strip('"').strip() or "conversation"
        found = index.search(scope, query, encoder=encoder, mode="semantic", limit=3)
        assert found["mode"] == "semantic" and 1 <= len(found["results"]) <= 3
        checked_passage(found["results"][0]["id"])
        assert index.search(denied, query, encoder=encoder, mode="semantic", limit=1)["results"] == []
        result["embedding"] = {key: coverage[key] for key in (
            "generation_id", "current_passages", "embedded_passages", "unique_texts",
            "embedded_unique_texts", "failed_unique_texts")}
        result["embedding"].update(semantic_results=len(found["results"]),
                                   passage_hash_valid=True, cross_owner_denied=True)

    if getattr(args, "check_knowledge", False):
        from aster_archive.knowledge import KnowledgeStore, PIPELINE_KEY

        knowledge = KnowledgeStore(store)
        coverage = knowledge.coverage(scope)
        records = sum(row["records"] for row in coverage["records"] if row["pipeline_key"] == PIPELINE_KEY)
        jobs = [row for row in coverage["jobs"] if row["pipeline_key"] == PIPELINE_KEY]
        completed = sum(row["revisions"] for row in jobs if row["status"] == "complete")
        failed = sum(row["revisions"] for row in jobs if row["status"] == "failed")
        assert records > 0 and completed > 0 and coverage["verified_decisions"] == 0
        where, parameters = _scope_sql(scope)
        with store.connection(readonly=True) as db:
            selected = db.execute(f"""SELECT k.record_id
                FROM aster_archive.knowledge_records k
                JOIN aster_archive.conversation_revisions r USING(revision_id)
                JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
                WHERE {where} AND c.current_revision=k.revision_id
                    AND k.pipeline_key=%s AND k.kind='summary' AND jsonb_array_length(k.citations)>0
                ORDER BY k.record_id LIMIT 1""", parameters + [PIPELINE_KEY]).fetchone()
        assert selected is not None
        record = knowledge.fetch(scope, selected["record_id"])
        metadata = record["metadata"]
        assert metadata["source_id"] == args.source and metadata["is_current_revision"] is True
        assert metadata["verified_decision"] is False and metadata["content_is_historical_evidence"] is True
        citation = record["citations"][0]
        passage = checked_passage(citation["passage_id"])
        start = citation["start_char"] - passage["metadata"]["start_char"]
        end = citation["end_char"] - passage["metadata"]["start_char"]
        assert 0 <= start < end <= len(passage["text"])
        assert passage["text"][start:end] == citation["exact_quote"]
        token = re.search(r"[^\W\d_]{4,}", record["text"], re.UNICODE)
        assert token is not None
        found = knowledge.search(scope, token.group(0), kind="summary", limit=3)["results"]
        assert 1 <= len(found) <= 3
        try:
            knowledge.fetch(denied, record["id"])
        except ArchiveNotFound:
            pass
        else:
            raise AssertionError("cross-owner knowledge fetch allowed")
        result["knowledge"] = {"pipeline_key": PIPELINE_KEY, "records": records,
                               "completed_revisions": completed, "failed_revisions": failed,
                               "search_results": len(found), "exact_citation_valid": True,
                               "passage_hash_valid": True, "cross_owner_denied": True}

    if getattr(args, "check_cognee", False):
        from aster_archive.cognee_bridge import coverage

        report = coverage(store, scope)
        completed = sum(row["passages"] for row in report["jobs"] if row["status"] == "complete")
        failed = sum(row["passages"] for row in report["jobs"] if row["status"] == "failed")
        summaries = sum(row["summaries"] for row in report["jobs"] if row["status"] == "complete")
        assert completed > 0 and report["graph_is_not_authoritative"] is True
        result["cognee"] = {"pipeline_key": report["pipeline_key"], "completed_passages": completed,
                            "failed_passages": failed, "derived_summaries": summaries,
                            "graph_is_not_authoritative": True}
    return result


def probe(args, expected=None):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from aster_archive.access import ArchiveScope, load_grants
    from aster_archive.store import ArchiveNotFound, PostgresArchive

    dsn = os.environ["ASTER_ARCHIVE_DATABASE_URL"]
    if args.database:
        if not re.fullmatch(r"aster_archive_restore_[a-z0-9_]{1,40}", args.database):
            raise ValueError("database override is limited to disposable restore targets")
        values = conninfo_to_dict(dsn)
        values["dbname"] = args.database
        dsn = make_conninfo(**values)
    store = PostgresArchive(dsn)
    with store.connection(readonly=True) as db:
        role = db.execute("""SELECT current_user AS role,
            current_setting('default_transaction_read_only') AS default_read_only,
            current_setting('transaction_read_only') AS transaction_read_only,
            pg_database_size(current_database()) AS database_bytes""").fetchone()
    assert role["role"] == "aster_archive_read"
    assert role["default_read_only"] == role["transaction_read_only"] == "on"
    receipt = store.import_report(args.import_id)
    assert (receipt["owner_id"], receipt["source_id"]) == (args.owner, args.source)
    assert receipt["status"] == "complete" and receipt["error_code"] is None
    assert receipt["finished_at"] is not None and receipt["records"].get("invalid", 0) == 0
    assert sum(receipt["records"].values()) > 0
    scope = ArchiveScope("local-recovery-check", args.owner, (args.source,), None)
    counts = store.coverage(scope)
    coverage = {key: counts[key] for key in (
        "conversations", "revisions", "exact_passages", "ambiguous_current_revisions",
        "earliest_source_creation", "latest_source_update", "complete_account_history_verified")}
    assert coverage["conversations"] > 0 and coverage["exact_passages"] > 0
    hits = store.search(scope, args.query, limit=3)["results"]
    assert 1 <= len(hits) <= 3
    selected = expected["passage"]["id"] if expected else hits[0]["id"]
    passage = store.fetch(scope, selected)

    def checked_metadata(item):
        meta, text = item["metadata"], item["text"]
        assert meta["source_id"] == args.source
        assert 0 <= meta["start_char"] <= meta["end_char"]
        assert len(text) == meta["end_char"] - meta["start_char"] <= 2000
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == meta["text_sha256"]
        return {"id": item["id"], **{key: meta[key] for key in (
            "revision_id", "node_id", "part_index", "start_char", "end_char", "text_sha256")}}

    selected_meta = checked_metadata(passage)
    assert passage["metadata"]["is_current_revision"] is True
    with store.connection(readonly=True) as db:
        raw = db.execute("""WITH selected AS (
            SELECT p.start_char,p.end_char,
                CASE WHEN jsonb_typeof(n.raw->'message'->'content'->'parts')='array'
                    THEN (n.raw->'message'->'content'->'parts')->p.part_index
                    WHEN p.part_index=0
                         AND jsonb_typeof(n.raw->'message'->'content'->'text')='string'
                    THEN n.raw->'message'->'content'->'text' END AS raw_part
            FROM aster_archive.passages p
            JOIN aster_archive.nodes n USING(revision_id,node_id)
            JOIN aster_archive.conversation_revisions r USING(revision_id)
            JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
            WHERE p.passage_id=%s AND c.owner_id=%s AND c.source_id=%s
                AND c.tombstoned_at IS NULL AND c.current_revision=p.revision_id)
            SELECT jsonb_typeof(raw_part) AS kind,
                char_length(raw_part #>> '{}') AS part_chars,
                substr(raw_part #>> '{}',start_char+1,end_char-start_char) AS source_slice
            FROM selected""", (selected, args.owner, args.source)).fetchone()
    assert raw and raw["kind"] == "string"
    assert selected_meta["end_char"] <= raw["part_chars"] and raw["source_slice"] == passage["text"]
    context = store.context(scope, selected, limit=3)
    assert 1 <= len(context["passages"]) <= 3
    assert context["revision_id"] == selected_meta["revision_id"]
    context_items = [checked_metadata(item) for item in context["passages"]]
    assert all(item["revision_id"] == selected_meta["revision_id"] for item in context_items)
    denied = ArchiveScope("local-recovery-denied", "denied-" + uuid.uuid4().hex, (args.source,), None)
    try:
        store.fetch(denied, selected)
    except ArchiveNotFound:
        pass
    else:
        raise AssertionError("cross-owner fetch allowed")
    clients = Path(os.environ["ASTER_ARCHIVE_CLIENTS"])
    grants = load_grants(clients)
    result = {
        "version": 1, "owner": args.owner, "source": args.source, "import_id": args.import_id,
        "coverage": coverage, "records": receipt["records"],
        "archive_sha256": receipt["archive_sha256"], "archive_bytes": receipt["size_bytes"],
        "search_ids": [item["id"] for item in hits], "passage": selected_meta,
        "context": {"passages": context_items, "ancestry_status": context["ancestry_status"],
                    "has_more": bool(context["next_cursor"])},
        "acl_sha256": file_hash(clients), "acl_client_count": len(grants),
        "reader_role": role["role"], "reader_default_read_only": True,
        "passage_hash_and_source_offsets_valid": True, "cross_owner_denied": True,
    }
    if expected is not None:
        assert result == {key: expected[key] for key in result}, "restored metadata differs"
    if any(getattr(args, "check_" + name, False) for name in ("embedding", "knowledge", "cognee")):
        result["enrichment"] = enrichment_probe(store, scope, args)
    result["database_bytes"] = role["database_bytes"]
    if args.database:
        digest = receipt["archive_sha256"]
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        original = Path(os.environ["ASTER_ARCHIVE_ROOT"]) / "objects" / "sha256" / digest[:2] / digest
        assert original.is_file() and not original.is_symlink()
        assert original.stat().st_size == receipt["size_bytes"] and file_hash(original) == digest
        auth = Path(os.environ["ASTER_ARCHIVE_OAUTH_DB"]).resolve(strict=True)
        with sqlite3.connect(auth.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        result.update(original_archive_hash_valid=True, restored_auth_valid=True, restored_acl_valid=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("capture", "verify", "extract"))
    parser.add_argument("--owner", default="owner-personal")
    parser.add_argument("--source", default="chatgpt-personal")
    parser.add_argument("--import-id")
    parser.add_argument("--query", default="ChatGPT")
    parser.add_argument("--database")
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
    parser.add_argument("--check-embedding", action="store_true")
    parser.add_argument("--check-knowledge", action="store_true")
    parser.add_argument("--check-cognee", action="store_true")
    args = parser.parse_args()
    if args.mode == "extract":
        assert args.max_bytes is not None and args.max_bytes > 0
        result = extract_stream(args.max_bytes, max_members=args.max_members)
    else:
        assert args.import_id is not None
        expected = json.load(sys.stdin) if args.mode == "verify" else None
        result = probe(args, expected)
    print(json.dumps({"result": "pass", **result}, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"result": "fail", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
