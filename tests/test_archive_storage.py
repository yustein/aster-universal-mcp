"""Disk headroom failures must preserve original data and resumable receipts."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from aster_archive import storage
from aster_archive.source import prepare_source
from aster_archive.storage import ArchiveStorageError, minimum_free_bytes, require_storage_reserve


def test_reserve_configuration_defaults_and_deployment_value():
    assert minimum_free_bytes(environ={}) == 1024**3
    assert minimum_free_bytes(environ={"ASTER_ARCHIVE_MIN_FREE_BYTES": "10737418240"}) == 10 * 1024**3
    assert minimum_free_bytes(environ={"ASTER_ARCHIVE_MIN_FREE_BYTES": str(1024**5)}) == 1024**5


@pytest.mark.parametrize("value", ["0", "-1", "10GiB", "1.5", "", str(1024**5 + 1), "9" * 100])
def test_invalid_reserve_configuration_fails_closed(value):
    with pytest.raises(ValueError, match="positive integer"):
        minimum_free_bytes(environ={"ASTER_ARCHIVE_MIN_FREE_BYTES": value})


def test_source_copy_reserves_its_full_size_and_can_resume_after_space_is_freed(tmp_path, monkeypatch):
    original = tmp_path / "source.json"
    original.write_text('[{"id":"a","mapping":{}}]', encoding="utf-8")
    archive = tmp_path / "archive"
    reserve = 1024
    monkeypatch.setenv("ASTER_ARCHIVE_MIN_FREE_BYTES", str(reserve))
    free = reserve + original.stat().st_size - 1
    checked_roots = []

    def disk_usage(path):
        checked_roots.append(path)
        return SimpleNamespace(free=free)

    monkeypatch.setattr(storage.shutil, "disk_usage", disk_usage)
    with pytest.raises(ArchiveStorageError) as failure:
        prepare_source(original, archive)
    assert isinstance(failure.value, OSError)
    assert checked_roots == [archive.resolve()]
    assert list(archive.iterdir()) == []  # No temporary copy or CAS publication.
    free += 1
    restored = prepare_source(original, archive)
    assert restored.path.read_bytes() == original.read_bytes()
    assert len(list(archive.glob("objects/sha256/*/*"))) == 1


def test_reserve_check_uses_configured_root_and_rejects_missing_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTER_ARCHIVE_MIN_FREE_BYTES", "100")
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=100))
    require_storage_reserve(tmp_path)
    with pytest.raises(ArchiveStorageError):
        require_storage_reserve(tmp_path, additional_bytes=1)
    with pytest.raises(OSError):
        require_storage_reserve(tmp_path / "missing")


@pytest.mark.skipif(not os.environ.get("ASTER_ARCHIVE_TEST_DSN"),
                    reason="requires a disposable real PostgreSQL ASTER_ARCHIVE_TEST_DSN")
def test_low_space_is_not_quarantined_and_stops_before_next_checkpoint(tmp_path, monkeypatch):
    from aster_archive.ingest import import_registered
    from aster_archive.store import PostgresArchive
    from aster_archive.watch import InboxWatcher

    store = PostgresArchive(os.environ["ASTER_ARCHIVE_TEST_DSN"])
    store.migrate()
    owner, source = "storage-integration-" + uuid.uuid4().hex, "synthetic-account"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    archive = tmp_path / "archive"
    records = [{"id": identity, "title": "Synthetic reserve test", "update_time": 20,
                "current_node": "message", "mapping": {"message": {
                    "parent": None, "children": [], "message": {"id": "message", "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": [identity]}}}}}
               for identity in ("conversation-one", "conversation-two")]
    (inbox / "export.json").write_text(json.dumps(records), encoding="utf-8")
    reserve = 1024 * 1024
    monkeypatch.setenv("ASTER_ARCHIVE_MIN_FREE_BYTES", str(reserve))
    free = reserve - 1
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=free))
    watcher = InboxWatcher(store, inbox=inbox, archive_root=archive, owner_id=owner, source_id=source)
    assert watcher.scan_once() == []
    assert watcher.scan_once() == [{"event": "source_retry_pending", "error_code": "source_io_error"}]
    with store.connection(readonly=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM aster_archive.inbox_receipts WHERE owner_id=%s", (owner,)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM aster_archive.imports WHERE owner_id=%s", (owner,)).fetchone()["n"] == 0
    assert not list(archive.glob("objects/sha256/*/*"))
    free = reserve * 2
    identity = watcher.scan_once()[0]["import_id"]

    checks = 0

    def fills_after_first_record(path):
        nonlocal checks
        checks += 1
        return SimpleNamespace(free=reserve * 2 if checks == 1 else reserve - 1)

    monkeypatch.setattr(storage.shutil, "disk_usage", fills_after_first_record)
    with pytest.raises(ArchiveStorageError):
        import_registered(store, identity, archive)
    failed = store.import_report(identity)
    assert failed["status"] == "failed" and failed["error_code"] == "ArchiveStorageError"
    assert failed["records"] == {"imported": 1}
    with store.connection(readonly=True) as conn:
        checkpoints = conn.execute("SELECT ordinal,status FROM aster_archive.import_items WHERE import_id=%s", (identity,)).fetchall()
        assert checkpoints == [{"ordinal": 0, "status": "imported"}]
        assert conn.execute("SELECT count(*) AS n FROM aster_archive.conversation_revisions WHERE owner_id=%s", (owner,)).fetchone()["n"] == 1
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=reserve * 2))
    resumed = import_registered(store, identity, archive)
    assert resumed["status"] == "complete" and resumed["records"] == {"imported": 2}
    with store.connection(readonly=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM aster_archive.conversation_revisions WHERE owner_id=%s", (owner,)).fetchone()["n"] == 2
    assert len(list(archive.glob("objects/sha256/*/*"))) == 1


@pytest.mark.skipif(not os.environ.get("ASTER_ARCHIVE_TEST_DSN"),
                    reason="requires a disposable real PostgreSQL ASTER_ARCHIVE_TEST_DSN")
def test_invalid_reserve_configuration_does_not_poison_pending_import(tmp_path, monkeypatch):
    from aster_archive import queue
    from aster_archive.store import PostgresArchive
    from aster_archive.watch import InboxWatcher

    dsn = os.environ["ASTER_ARCHIVE_TEST_DSN"]
    store = PostgresArchive(dsn)
    store.migrate()
    queue.migrate_queue(dsn)
    owner = "storage-config-" + uuid.uuid4().hex
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    archive = tmp_path / "archive"
    path = inbox / "export.json"
    path.write_text(json.dumps([{
        "id": "valid-pending-conversation", "title": "Synthetic configuration test",
        "update_time": 20, "current_node": "message", "mapping": {"message": {
            "parent": None, "children": [], "message": {"id": "message",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["Valid pending source"]}}}},
    }]), encoding="utf-8")
    monkeypatch.setenv("ASTER_ARCHIVE_MIN_FREE_BYTES", "1024")
    source = prepare_source(path, archive)
    import_id = store.register_import(source, owner, "synthetic-account")

    def must_not_open_worker(*args, **kwargs):
        raise AssertionError("worker opened before reserve configuration validation")

    with monkeypatch.context() as invalid:
        invalid.setenv("ASTER_ARCHIVE_MIN_FREE_BYTES", "10GiB")
        invalid.setattr(queue.procrastinate.App, "open_async", must_not_open_worker)
        with pytest.raises(ValueError, match="positive integer"):
            queue.run_worker(dsn, archive)
        with pytest.raises(ValueError, match="positive integer"):
            queue.dispatch_pending(dsn, archive)
        with pytest.raises(ValueError, match="positive integer"):
            InboxWatcher(store, inbox=inbox, archive_root=archive,
                         owner_id=owner, source_id="synthetic-account")

    with store.connection(readonly=True) as conn:
        state = conn.execute("SELECT status,error_code FROM aster_archive.imports WHERE import_id=%s",
                             (import_id,)).fetchone()
        assert state == {"status": "pending", "error_code": None}
        outbox = conn.execute("SELECT dispatched_job_id,dispatched_at FROM aster_archive.import_outbox WHERE import_id=%s",
                              (import_id,)).fetchone()
        assert outbox == {"dispatched_job_id": None, "dispatched_at": None}
        assert conn.execute("SELECT count(*) AS n FROM aster_archive.import_dead_letters WHERE import_id=%s",
                            (import_id,)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM public.procrastinate_jobs WHERE args->>'import_id'=%s",
                            (import_id,)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM aster_archive.inbox_receipts WHERE owner_id=%s",
                            (owner,)).fetchone()["n"] == 0
    # Correcting the configuration admits the same pending source; no new export
    # or manual repair of invalid-source/dead-letter state is necessary.
    assert queue.IMPORT_TASK in queue.build_app(dsn, archive).tasks
    InboxWatcher(store, inbox=inbox, archive_root=archive,
                 owner_id=owner, source_id="synthetic-account")
