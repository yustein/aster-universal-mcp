"""Queue delivery/recovery tests; PostgreSQL cases use only unique synthetic IDs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import procrastinate.jobs
import psycopg
from psycopg.rows import dict_row
import pytest

from aster_archive import queue

PID = "a" * 64


def job(attempts=0, job_id=1):
    return procrastinate.jobs.Job(
        id=job_id, attempts=attempts, queue=queue.IMPORT_QUEUE,
        task_name=queue.IMPORT_TASK, task_kwargs={"import_id": PID},
        lock=queue.IMPORT_LOCK, queueing_lock=None,
    )


def test_pinned_app_keeps_import_serial_and_maintenance_available(tmp_path):
    app = queue.build_app("postgresql://fake@/not-connected", tmp_path)
    task = app.tasks[queue.IMPORT_TASK]
    assert task.queue == queue.IMPORT_QUEUE and task.lock == queue.IMPORT_LOCK
    maintenance = app.tasks[queue.MAINTENANCE_TASK]
    assert maintenance.queue == queue.MAINTENANCE_QUEUE and maintenance.lock is None
    assert app.worker_defaults["concurrency"] == 2
    assert app.worker_defaults["update_heartbeat_interval"] == 10
    assert app.worker_defaults["delete_jobs"] == "never"
    assert (queue.MAINTENANCE_TASK, "archive-heartbeat-recovery-v1") in app.periodic_registry.periodic_tasks
    retry = task.retry_strategy
    for previous, expected in ((0, 30), (1, 60)):
        before = datetime.now(timezone.utc)
        decision = retry.get_retry_decision(exception=queue.TransientImportFailure(), job=job(previous))
        assert expected - 1 <= (decision.retry_at - before).total_seconds() <= expected + 1
    assert retry.get_retry_decision(exception=queue.TransientImportFailure(), job=job(2)) is None
    assert retry.get_retry_decision(exception=queue.PermanentImportFailure(), job=job(0)) is None


@pytest.mark.parametrize("code,previous,error,dead_reason", [
    (20, 0, queue.PermanentImportFailure, "invalid_source"),
    (22, 0, queue.PermanentImportFailure, "import_unexpected_failure"),
    (21, 0, queue.TransientImportFailure, None),
    (21, 2, queue.TransientImportFailure, "transient_attempts_exhausted"),
    (-9, 2, queue.TransientImportFailure, "transient_attempts_exhausted"),
])
def test_failure_classification_and_bounded_dead_letter(tmp_path, monkeypatch, code, previous, error, dead_reason):
    receipts = []
    async def child(*args):
        return code
    monkeypatch.setattr(queue, "_import_subprocess", child)
    monkeypatch.setattr(queue, "_dead_letter", lambda *args: receipts.append(args))
    app = queue.build_app("private-dsn", tmp_path)
    with pytest.raises(error) as failure:
        asyncio.run(app.tasks[queue.IMPORT_TASK](SimpleNamespace(job=job(previous)), import_id=PID))
    assert "private-dsn" not in str(failure.value)
    if dead_reason:
        assert receipts == [("private-dsn", PID, 1, dead_reason, previous + 1)]
    else:
        assert receipts == []


def test_reaper_uses_heartbeat_and_exact_owned_queue_task_filters(monkeypatch):
    calls, recovered = [], []
    class Manager:
        async def get_stalled_jobs(self, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(id=len(calls))]
    monkeypatch.setattr(queue, "_recover_stalled", lambda dsn, job_id: recovered.append(job_id) or True)
    assert asyncio.run(queue.recover_stalled(SimpleNamespace(job_manager=Manager()), "dsn")) == 2
    assert calls == [
        {"queue": queue.IMPORT_QUEUE, "task_name": queue.IMPORT_TASK, "seconds_since_heartbeat": 120},
        {"queue": queue.MAINTENANCE_QUEUE, "task_name": queue.MAINTENANCE_TASK, "seconds_since_heartbeat": 120},
    ]
    assert recovered == [1, 2]


def test_dsn_is_kept_out_of_subprocess_argv_and_output(tmp_path, monkeypatch):
    captured = {}
    class Process:
        returncode = 0
        async def wait(self):
            return self.returncode
    async def create(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert asyncio.run(queue._import_subprocess("postgresql://secret", PID, tmp_path)) == 0
    assert "postgresql://secret" not in " ".join(captured["args"])
    assert captured["kwargs"]["env"]["ASTER_ARCHIVE_DATABASE_URL"] == "postgresql://secret"
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == asyncio.subprocess.DEVNULL


@pytest.fixture
def pg(tmp_path):
    dsn = os.environ.get("ASTER_ARCHIVE_TEST_DSN")
    if not dsn:
        pytest.skip("Set ASTER_ARCHIVE_TEST_DSN to a disposable PostgreSQL database")
    from aster_archive.store import PostgresArchive
    from aster_archive.source import prepare_source

    store = PostgresArchive(dsn)
    store.migrate()
    queue.migrate_queue(dsn)
    archive_root = tmp_path / "archive"
    # This explicitly disposable database can contain receipts from earlier
    # integration runs. Drain their delivery obligations before registering the
    # subject of a one-batch atomicity test; dispatch intentionally caps at100.
    # No job executes here and no source/canonical record is removed.
    while queue.dispatch_pending(dsn, archive_root):
        pass
    owner = "queue-test-" + uuid.uuid4().hex
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([{
        "id": "queue-conversation", "title": "Queue test", "create_time": 1, "update_time": 2,
        "current_node": "user-node", "mapping": {
            "root": {"id": "root", "parent": None, "children": ["user-node"], "message": None},
            "user-node": {"id": "user-node", "parent": "root", "children": [], "message": {
                "id": "user-message", "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["queue exact source"]},
            }},
        },
    }]), encoding="utf-8")
    source = prepare_source(path, archive_root)
    import_id = store.register_import(source, owner, "personal")
    return SimpleNamespace(dsn=dsn, store=store, owner=owner, import_id=import_id,
                           root=archive_root)


def test_pg_outbox_enqueue_ack_are_atomic_on_real_connector(pg, monkeypatch):
    original = procrastinate.jobs.JobDeferrer.defer
    def interrupted(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("simulated crash between queue insertion and outbox ack")
    monkeypatch.setattr(procrastinate.jobs.JobDeferrer, "defer", interrupted)
    with pytest.raises(RuntimeError, match="simulated crash"):
        queue.dispatch_pending(pg.dsn, pg.root)
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        receipt = db.execute("SELECT * FROM aster_archive.import_outbox WHERE import_id=%s", (pg.import_id,)).fetchone()
        assert receipt["dispatched_job_id"] is None
        assert db.execute("SELECT count(*) AS n FROM public.procrastinate_jobs WHERE args->>'import_id'=%s", (pg.import_id,)).fetchone()["n"] == 0
    monkeypatch.setattr(procrastinate.jobs.JobDeferrer, "defer", original)
    queue.dispatch_pending(pg.dsn, pg.root)
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        receipt = db.execute("SELECT * FROM aster_archive.import_outbox WHERE import_id=%s", (pg.import_id,)).fetchone()
        queued = db.execute("SELECT * FROM public.procrastinate_jobs WHERE id=%s", (receipt["dispatched_job_id"],)).fetchone()
        assert queued["args"] == {"import_id": pg.import_id}
        assert queued["queue_name"] == queue.IMPORT_QUEUE and queued["lock"] == queue.IMPORT_LOCK
    queue.dispatch_pending(pg.dsn, pg.root)
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        assert db.execute("SELECT count(*) AS n FROM public.procrastinate_jobs WHERE args->>'import_id'=%s", (pg.import_id,)).fetchone()["n"] == 1


def test_pg_stale_heartbeat_recovers_only_owned_stalled_jobs_and_exhausts(pg):
    queue.dispatch_pending(pg.dsn, pg.root)
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        job_id = db.execute("SELECT dispatched_job_id FROM aster_archive.import_outbox WHERE import_id=%s", (pg.import_id,)).fetchone()["dispatched_job_id"]
        worker_id = db.execute("INSERT INTO public.procrastinate_workers DEFAULT VALUES RETURNING id").fetchone()["id"]
        db.execute("UPDATE public.procrastinate_jobs SET status='doing',worker_id=%s WHERE id=%s", (worker_id, job_id))
    assert queue._recover_stalled(pg.dsn, job_id) is False
    with psycopg.connect(pg.dsn) as db:
        db.execute("UPDATE public.procrastinate_workers SET last_heartbeat=now()-interval '5 minutes' WHERE id=%s", (worker_id,))
    assert queue._recover_stalled(pg.dsn, job_id) is True
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        row = db.execute("SELECT status,attempts FROM public.procrastinate_jobs WHERE id=%s", (job_id,)).fetchone()
        assert row == {"status": "todo", "attempts": 1}
        db.execute("UPDATE public.procrastinate_jobs SET status='doing',attempts=2 WHERE id=%s", (job_id,))
    assert queue._recover_stalled(pg.dsn, job_id) is True
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        row = db.execute("SELECT status,attempts FROM public.procrastinate_jobs WHERE id=%s", (job_id,)).fetchone()
        assert row == {"status": "failed", "attempts": 3}
        dead = db.execute("SELECT reason,attempts FROM aster_archive.import_dead_letters WHERE import_id=%s", (pg.import_id,)).fetchone()
        assert dead == {"reason": "stalled_attempts_exhausted", "attempts": 3}
    assert pg.store.get_import(pg.import_id)["status"] == "failed"
    assert queue._recover_stalled(pg.dsn, job_id) is False


def test_pg_committed_import_recovers_missing_job_completion_without_reimport(pg):
    queue.dispatch_pending(pg.dsn, pg.root)
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        job_id = db.execute("SELECT dispatched_job_id FROM aster_archive.import_outbox WHERE import_id=%s", (pg.import_id,)).fetchone()["dispatched_job_id"]
        db.execute("UPDATE public.procrastinate_jobs SET status='doing',worker_id=NULL WHERE id=%s", (job_id,))
        db.execute("UPDATE aster_archive.imports SET status='complete' WHERE import_id=%s", (pg.import_id,))
    assert queue._recover_stalled(pg.dsn, job_id) is True
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        assert db.execute("SELECT status FROM public.procrastinate_jobs WHERE id=%s", (job_id,)).fetchone()["status"] == "succeeded"
        assert db.execute("SELECT count(*) AS n FROM aster_archive.import_dead_letters WHERE import_id=%s", (pg.import_id,)).fetchone()["n"] == 0


def test_pg_real_worker_imports_registered_snapshot_in_child(pg):
    pytest.importorskip("aster_archive.ingest")
    app = queue.build_app(pg.dsn, pg.root)
    test_queue = "test_" + uuid.uuid4().hex[:20]
    async def run():
        async with app.open_async():
            job_id = await app.tasks[queue.IMPORT_TASK].configure(queue=test_queue, lock=test_queue + ".serial").defer_async(import_id=pg.import_id)
            await app.run_worker_async(queues=[test_queue], wait=False, concurrency=1, install_signal_handlers=False)
            return job_id
    job_id = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert pg.store.get_import(pg.import_id)["status"] == "complete"
    with psycopg.connect(pg.dsn, row_factory=dict_row) as db:
        assert db.execute("SELECT status FROM public.procrastinate_jobs WHERE id=%s", (job_id,)).fetchone()["status"] == "succeeded"


def test_graceful_shutdown_cancellation_remains_resumable(tmp_path, monkeypatch):
    from procrastinate.job_context import AbortReason
    async def stopped_child(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(queue, "_import_subprocess", stopped_child)
    app = queue.build_app("private-dsn", tmp_path)
    context = SimpleNamespace(job=job(0), abort_reason=lambda: AbortReason.SHUTDOWN)
    with pytest.raises(queue.TransientImportFailure, match="ARCHIVE_IMPORT_TRANSIENT"):
        asyncio.run(app.tasks[queue.IMPORT_TASK](context, import_id=PID))
    assert app.tasks[queue.IMPORT_TASK].retry_strategy.get_retry_decision(
        exception=queue.TransientImportFailure(), job=context.job) is not None
