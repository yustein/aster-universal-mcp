"""Resumable, deterministic ingestion. No model/API calls are made here."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re

import psycopg
from psycopg.types.json import Jsonb

from .normalize import NormalizationError, normalize_conversation
from .source import load_source, prepare_source, iter_conversations
from .store import PostgresArchive
from .storage import require_storage_reserve


def import_registered(store: PostgresArchive, import_id: str, archive_root: Path) -> dict:
    receipt = store.get_import(import_id)
    if receipt["status"] in ("complete", "complete_with_errors"):
        return store.import_report(import_id)
    with store.import_lock(receipt["owner_id"], receipt["source_id"]) as conn:
        current = conn.execute("SELECT status FROM aster_archive.imports WHERE import_id=%s", (import_id,)).fetchone()
        if current["status"] in ("complete", "complete_with_errors"):
            return store.import_report(import_id)
        conn.execute("""UPDATE aster_archive.imports SET status='running',started_at=now(),
            finished_at=NULL,error_code=NULL WHERE import_id=%s""", (import_id,))
        try:
            root = Path(archive_root).resolve(strict=True)
            source_path = Path(receipt["object_path"]).resolve(strict=True)
            if not source_path.is_relative_to(root):
                raise ValueError("registered source is outside the configured archive root")
            source = load_source(source_path, receipt["archive_sha256"])
            for member, ordinal, raw in iter_conversations(source):
                if store.item_exists(conn, import_id, member, ordinal):
                    continue
                try:
                    normalized = normalize_conversation(raw)
                    require_storage_reserve(root)
                    store.commit_item(conn, import_id, member, ordinal, normalized)
                except NormalizationError:
                    require_storage_reserve(root)
                    store.commit_invalid(conn, import_id, member, ordinal, "unsupported_conversation")
                except psycopg.DataError:
                    # For example PostgreSQL JSONB cannot represent a NUL
                    # character. Preserve the original and report the gap.
                    require_storage_reserve(root)
                    store.commit_invalid(conn, import_id, member, ordinal, "unrepresentable_database_value")
            with conn.transaction():
                errors = conn.execute("""SELECT count(*) AS n FROM aster_archive.import_items
                    WHERE import_id=%s AND status='invalid'""", (import_id,)).fetchone()["n"]
                inventory = []
                for original in source.inventory:
                    item = dict(original)
                    name = PurePosixPath(item["name"]).name
                    if name == "conversations.json" or re.fullmatch(r"conversations-\d+\.json", name):
                        item["parse_complete"] = True
                        if source.kind == "zip":
                            item["crc_verified"] = True
                    inventory.append(item)
                conn.execute("""UPDATE aster_archive.imports SET status=%s,finished_at=now(),inventory=%s,
                    error_code=%s WHERE import_id=%s""",
                             ("complete_with_errors" if errors else "complete", Jsonb(inventory),
                              "record_errors" if errors else None, import_id))
        except BaseException as error:
            # Never persist source text, filesystem exceptions or DSN values in
            # errors. Keyboard interruption is also a resumable failed receipt.
            if not conn.closed:
                conn.execute("""UPDATE aster_archive.imports SET status='failed',finished_at=now(),
                    error_code=%s WHERE import_id=%s""", (type(error).__name__[:80], import_id))
            raise
    return store.import_report(import_id)


def import_path(store: PostgresArchive, path: Path, archive_root: Path,
                owner_id: str, source_id: str) -> dict:
    source = prepare_source(Path(path), Path(archive_root))
    import_id = store.register_import(source, owner_id, source_id)
    return import_registered(store, import_id, Path(archive_root))
