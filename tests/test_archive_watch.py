"""Completed-file acquisition and durable replay against real PostgreSQL."""
import json
import os
from pathlib import Path
import uuid
import zipfile

import psycopg
from psycopg.rows import dict_row
import pytest

from aster_archive.store import PostgresArchive
from aster_archive.watch import InboxWatcher


DSN = os.environ.get("ASTER_ARCHIVE_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires a disposable real PostgreSQL ASTER_ARCHIVE_TEST_DSN")


@pytest.fixture(scope="module")
def store():
    result = PostgresArchive(DSN)
    result.migrate()
    return result


@pytest.fixture
def acquisition(store, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    values = {
        "inbox": inbox, "archive_root": tmp_path / "archive",
        "owner_id": "watch-integration-" + uuid.uuid4().hex, "source_id": "synthetic-account",
    }
    return values


def rows(table, acquisition):
    assert table in {"imports", "inbox_receipts"}
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return conn.execute(f"SELECT * FROM aster_archive.{table} WHERE owner_id=%s AND source_id=%s",
                            (acquisition["owner_id"], acquisition["source_id"])).fetchall()


def delivery_count(import_id):
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT count(*) FROM aster_archive.import_outbox WHERE import_id=%s", (import_id,)).fetchone()[0]


def write_json(path, identity="a"):
    path.write_text(json.dumps([{"id": identity, "mapping": {}}]), encoding="utf-8")
    return path


def test_waits_for_two_stable_scans_and_ignores_partials_links_and_directories(store, acquisition):
    inbox = acquisition["inbox"]
    partial = inbox / "export.zip.partial"
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("conversations.json", '[{"id":"a","mapping":{}}]')
    external = write_json(inbox.parent / "external.json")
    (inbox / "linked.json").symlink_to(external)
    (inbox / "folder.json").mkdir()
    watcher = InboxWatcher(store, **acquisition)
    assert watcher.scan_once() == []
    assert watcher.scan_once() == []
    assert rows("imports", acquisition) == []
    partial.rename(inbox / "export.zip")
    assert watcher.scan_once() == []
    events = watcher.scan_once()
    assert len(events) == 1 and events[0]["event"] == "import_registered"
    imported = rows("imports", acquisition)
    assert len(imported) == 1 and imported[0]["status"] == "pending"
    assert delivery_count(imported[0]["import_id"]) == 1
    assert len(rows("inbox_receipts", acquisition)) == 1


def test_restart_reuses_durable_inbox_receipt(store, acquisition):
    write_json(acquisition["inbox"] / "export.json")
    first = InboxWatcher(store, **acquisition)
    assert first.scan_once() == []
    event = first.scan_once()[0]
    restarted = InboxWatcher(store, **acquisition)
    assert restarted.scan_once() == []
    assert restarted.scan_once() == []
    assert len(rows("imports", acquisition)) == 1
    assert len(rows("inbox_receipts", acquisition)) == 1
    assert delivery_count(event["import_id"]) == 1
    assert len(list(acquisition["archive_root"].glob("objects/sha256/*/*"))) == 1


def test_replaced_name_with_same_size_and_preserved_mtime_is_rehashed(store, acquisition):
    path = write_json(acquisition["inbox"] / "export.json", identity="a")
    watcher = InboxWatcher(store, **acquisition)
    watcher.scan_once()
    first = watcher.scan_once()[0]
    original = path.stat()
    replacement = write_json(acquisition["inbox"] / "replacement.partial", identity="b")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(path)
    current = path.stat()
    assert current.st_size == original.st_size and current.st_mtime_ns == original.st_mtime_ns
    assert current.st_ino != original.st_ino
    assert watcher.scan_once() == []
    second = watcher.scan_once()[0]
    assert second["import_id"] != first["import_id"]
    assert len(rows("imports", acquisition)) == 2
    assert len(rows("inbox_receipts", acquisition)) == 2


def test_crash_after_registration_before_inbox_receipt_replays_safely(store, acquisition, monkeypatch):
    write_json(acquisition["inbox"] / "export.json")
    watcher = InboxWatcher(store, **acquisition)
    watcher.scan_once()
    actual_receipt = store.record_inbox

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash before inbox acknowledgement")

    monkeypatch.setattr(store, "record_inbox", crash)
    with pytest.raises(RuntimeError):
        watcher.scan_once()
    imports = rows("imports", acquisition)
    assert len(imports) == 1 and delivery_count(imports[0]["import_id"]) == 1
    assert rows("inbox_receipts", acquisition) == []
    monkeypatch.setattr(store, "record_inbox", actual_receipt)
    restarted = InboxWatcher(store, **acquisition)
    assert restarted.scan_once() == []
    event = restarted.scan_once()[0]
    assert event["import_id"] == imports[0]["import_id"]
    assert len(rows("imports", acquisition)) == 1
    assert delivery_count(event["import_id"]) == 1
    assert len(rows("inbox_receipts", acquisition)) == 1


def test_transient_source_io_failure_is_retried_without_skip_receipt(store, acquisition, monkeypatch):
    from aster_archive import watch
    write_json(acquisition["inbox"] / "export.json")
    watcher = InboxWatcher(store, **acquisition)
    actual_prepare = watch.prepare_source
    attempts = 0

    def transient(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic transient read failure")
        return actual_prepare(*args, **kwargs)

    monkeypatch.setattr(watch, "prepare_source", transient)
    assert watcher.scan_once() == []
    assert watcher.scan_once() == [{"event": "source_retry_pending", "error_code": "source_io_error"}]
    assert rows("inbox_receipts", acquisition) == []
    assert rows("imports", acquisition) == []
    assert watcher.scan_once()[0]["event"] == "import_registered"
    assert len(rows("inbox_receipts", acquisition)) == 1
