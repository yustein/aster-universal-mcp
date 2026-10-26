"""Local operator CLI. Import/deletion commands are never exposed over MCP."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys

from .ingest import import_path
from .store import PostgresArchive


def _json(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("unsupported result type")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Aster source-backed ChatGPT conversation archive")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="install the pinned archive and job schemas")
    for command in ("import", "watch", "status", "tombstone"):
        c = sub.add_parser(command)
        c.add_argument("--owner", required=True, help="stable owner identity")
        c.add_argument("--source", required=True, help="stable account/workspace source identity")
        if command in ("import", "watch"):
            c.add_argument("--archive-root", type=Path,
                           default=Path(os.environ.get("ASTER_ARCHIVE_ROOT", "/var/lib/aster-archive")))
        if command == "import":
            c.add_argument("path", type=Path)
        elif command == "watch":
            c.add_argument("--inbox", type=Path, required=True)
            c.add_argument("--poll-seconds", type=int, default=60)
        elif command == "tombstone":
            c.add_argument("conversation_id", help="suppress retrieval and future reimport; retains raw originals")
    w = sub.add_parser("worker", help="run bounded ingestion and heartbeat recovery")
    w.add_argument("--archive-root", type=Path,
                   default=Path(os.environ.get("ASTER_ARCHIVE_ROOT", "/var/lib/aster-archive")))
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    dsn = os.environ.get("ASTER_ARCHIVE_DATABASE_URL", "")
    try:
        store = PostgresArchive(dsn)
        flag = os.environ.get("ASTER_ARCHIVE_ENABLE_ENRICHMENT", "0")
        if flag not in {"0", "1"}:
            raise ValueError("archive enrichment flag must be 0 or 1")
        if args.command == "migrate":
            from .queue import migrate_queue
            store.migrate()
            migrate_queue(dsn)
            result = {"status": "schemas_ready"}
            if flag == "1":
                from .embedding_store import EmbeddingStore
                from .knowledge import KnowledgeStore
                # The database operator installs pgvector separately. The
                # ingest role applies additive, checksummed component schemas.
                EmbeddingStore(store).migrate()
                KnowledgeStore(store).migrate()
                result["enrichment"] = "schemas_ready"
        elif args.command == "import":
            result = import_path(store, args.path, args.archive_root, args.owner, args.source)
        elif args.command == "status":
            result = store.status(args.owner, args.source)
            if flag == "1":
                from .access import ArchiveScope
                from .embedding_store import EmbeddingStore
                from .knowledge import KnowledgeStore
                from .cognee_bridge import coverage as cognee_coverage

                scope = ArchiveScope("local-operator", args.owner, (args.source,), None)
                result["embeddings"] = EmbeddingStore(store).coverage(scope)
                result["knowledge_extraction"] = KnowledgeStore(store).coverage(scope)
                result["cognee"] = cognee_coverage(store, scope)
        elif args.command == "tombstone":
            store.tombstone(args.owner, args.source, args.conversation_id)
            result = {"status": "tombstoned", "raw_originals_retained": True}
        elif args.command == "watch":
            from .watch import run_watch
            run_watch(store, inbox=args.inbox, archive_root=args.archive_root,
                      owner_id=args.owner, source_id=args.source, poll_seconds=args.poll_seconds)
            return 0
        else:
            from .queue import run_worker
            run_worker(dsn, args.archive_root)
            return 0
        print(json.dumps(result, default=_json, ensure_ascii=False))
        return 0 if result.get("status") != "complete_with_errors" else 2
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(json.dumps({"status": "failed", "error_code": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
