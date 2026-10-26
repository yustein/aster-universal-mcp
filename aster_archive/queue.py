"""Pinned PostgreSQL job delivery, bounded imports and heartbeat recovery.

Import registration and its outbox obligation share the archive transaction.
The pinned Procrastinate connector also accepts our existing psycopg connection,
so dispatch commits its queue row and outbox acknowledgement together. Import
identity/checkpoints still make replay safe; no external exactly-once claim is
made. Queue arguments contain opaque import IDs only, never conversation text.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import os
from pathlib import Path
import re
import signal
import sys

import procrastinate
from procrastinate.schema import SchemaManager
from procrastinate.job_context import AbortReason
import psycopg
from psycopg.rows import dict_row

PINNED_QUEUE_VERSION = "3.10.0"
IMPORT_QUEUE = "aster_archive_import"
MAINTENANCE_QUEUE = "aster_archive_maintenance"
IMPORT_TASK = "aster_archive.import"
MAINTENANCE_TASK = "aster_archive.maintenance"
IMPORT_LOCK = "aster_archive.import.serial"
MAX_ATTEMPTS = 3
HEARTBEAT_SECONDS = 10
STALLED_SECONDS = 120
IMPORT_TIMEOUT_SECONDS = 6 * 60 * 60
IMPORT_ID = re.compile(r"[a-f0-9]{64}\Z")
_MIGRATION_LOCK = int.from_bytes(hashlib.sha256(b"aster.archive.queue.schema.v1").digest()[:8], "big", signed=True)
_CONNECT_OPTIONS = "-c search_path=public -c statement_timeout=15000 -c lock_timeout=5000"


class QueueError(RuntimeError):
    """Sanitized operator error; never includes a DSN or source content."""


class TransientImportFailure(QueueError):
    pass


class PermanentImportFailure(QueueError):
    pass


def _version_check():
    if importlib.metadata.version("procrastinate") != PINNED_QUEUE_VERSION:
        raise QueueError("ARCHIVE_QUEUE_VERSION_MISMATCH")


def _connect(dsn):
    return psycopg.connect(dsn, connect_timeout=10, options=_CONNECT_OPTIONS, row_factory=dict_row)


def migrate_queue(dsn: str) -> None:
    """Install the exact SDK schema once; do not adopt or replace unknown state."""
    _version_check()
    schema = SchemaManager.get_schema()
    checksum = hashlib.sha256(schema.encode("utf-8")).hexdigest()
    with _connect(dsn) as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        if db.execute("SELECT to_regnamespace('aster_archive') AS schema").fetchone()["schema"] is None:
            raise QueueError("ARCHIVE_SCHEMA_REQUIRED")
        marker = db.execute("SELECT to_regclass('aster_archive.queue_schema') AS marker").fetchone()["marker"]
        if marker:
            rows = db.execute("SELECT version, checksum FROM aster_archive.queue_schema").fetchall()
            if rows != [{"version": PINNED_QUEUE_VERSION, "checksum": checksum}]:
                raise QueueError("ARCHIVE_QUEUE_SCHEMA_MISMATCH")
            if db.execute("SELECT to_regclass('public.procrastinate_jobs') AS jobs").fetchone()["jobs"] is None:
                raise QueueError("ARCHIVE_QUEUE_SCHEMA_MISMATCH")
            return
        # Never run CREATE OR REPLACE over somebody else's queue functions.
        occupied = db.execute("""
            SELECT EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND starts_with(c.relname, 'procrastinate_')
                UNION ALL
                SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname='public' AND starts_with(p.proname, 'procrastinate_')
                UNION ALL
                SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
                WHERE n.nspname='public' AND starts_with(t.typname, 'procrastinate_')
            ) AS occupied
        """).fetchone()["occupied"]
        if occupied:
            raise QueueError("ARCHIVE_QUEUE_UNRECOGNIZED_EXISTING_SCHEMA")
        # This is the installed, version-pinned package's schema, not interpolated
        # operator input. Unlike apply_schema(), this uses our transaction so the
        # marker cannot be committed separately from the queue objects.
        db.execute(schema)
        db.execute("""CREATE TABLE aster_archive.queue_schema (
            version text PRIMARY KEY, checksum text NOT NULL,
            installed_at timestamptz NOT NULL DEFAULT now())""")
        db.execute("INSERT INTO aster_archive.queue_schema(version,checksum) VALUES (%s,%s)",
                   (PINNED_QUEUE_VERSION, checksum))
        db.execute("""CREATE TABLE aster_archive.import_dead_letters (
            import_id text PRIMARY KEY REFERENCES aster_archive.imports,
            job_id bigint NOT NULL, reason text NOT NULL,
            attempts integer NOT NULL CHECK(attempts>0),
            recorded_at timestamptz NOT NULL DEFAULT now())""")


def _record_dead_letter(db, import_id, job_id, reason, attempts):
    # Lock the canonical import before marking it failed. A completion that
    # committed before a lost job acknowledgement must stay completed.
    row = db.execute("SELECT status FROM aster_archive.imports WHERE import_id=%s FOR UPDATE",
                     (import_id,)).fetchone()
    if row is None or row["status"] in ("complete", "complete_with_errors"):
        return
    db.execute("""INSERT INTO aster_archive.import_dead_letters(import_id,job_id,reason,attempts)
                  VALUES (%s,%s,%s,%s) ON CONFLICT(import_id) DO NOTHING""",
               (import_id, job_id, reason, attempts))
    db.execute("""UPDATE aster_archive.imports SET status='failed',error_code=%s,finished_at=now()
                  WHERE import_id=%s""", (reason, import_id))


def _dead_letter(dsn, import_id, job_id, reason, attempts):
    with _connect(dsn) as db:
        _record_dead_letter(db, import_id, job_id, reason, attempts)


def _run_import_once(dsn: str, import_id: str, archive_root: str) -> int:
    """Child-only adapter; return a small classification, never exception text."""
    if not IMPORT_ID.fullmatch(import_id):
        return 20
    from .store import ArchiveBusy
    try:
        with _connect(dsn) as db:
            row = db.execute("""SELECT i.status, (d.import_id IS NOT NULL) AS dead
                FROM aster_archive.imports i LEFT JOIN aster_archive.import_dead_letters d USING(import_id)
                WHERE i.import_id=%s""", (import_id,)).fetchone()
        if row is None:
            return 20
        if row["status"] in ("complete", "complete_with_errors") or row["dead"]:
            return 0
        from .store import PostgresArchive
        from .ingest import import_registered
        import_registered(PostgresArchive(dsn), import_id, Path(archive_root))
        return 0
    except (ValueError, LookupError):
        return 20
    except (OSError, psycopg.OperationalError, psycopg.InterfaceError, ArchiveBusy):
        return 21
    except Exception:
        # Unexpected defects require investigation instead of repeatedly parsing
        # the same corpus. SQL/data bugs are not presumed transient.
        return 22


async def _import_subprocess(dsn: str, import_id: str, archive_root: Path) -> int:
    env = dict(os.environ)
    env["ASTER_ARCHIVE_DATABASE_URL"] = dsn
    env["ASTER_ARCHIVE_PARENT_PID"] = str(os.getpid())
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "aster_archive.queue", "_import", import_id, str(archive_root),
        env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=IMPORT_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise


def _arm_worker_parent() -> None:
    """Stop the Linux import child if its worker dies, including SIGKILL.

    A post-prctl identity check covers a worker exit before the signal was
    armed. This is child-only and never changes the operator/worker process.
    """
    expected = os.environ.get("ASTER_ARCHIVE_PARENT_PID")
    if expected is None:
        return
    if sys.platform != "linux" or not expected.isdecimal():
        raise QueueError("ARCHIVE_CHILD_PARENT_UNSUPPORTED")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:
        raise QueueError("ARCHIVE_CHILD_PARENT_GUARD_FAILED")
    if os.getppid() != int(expected):
        raise QueueError("ARCHIVE_CHILD_PARENT_EXITED")


def _recover_stalled(dsn: str, job_id: int) -> bool:
    """Recheck a heartbeat candidate under locks before changing its state."""
    with _connect(dsn) as db:
        row = db.execute("""SELECT id,task_name,queue_name,status,attempts,args,worker_id,abort_requested
            FROM public.procrastinate_jobs WHERE id=%s FOR UPDATE""", (job_id,)).fetchone()
        if row is None or row["status"] != "doing":
            return False
        if (row["queue_name"], row["task_name"]) not in (
            (IMPORT_QUEUE, IMPORT_TASK), (MAINTENANCE_QUEUE, MAINTENANCE_TASK),
        ):
            return False
        if row["worker_id"] is not None:
            worker = db.execute("""SELECT last_heartbeat > now() - (%s * interval '1 second') AS live
                FROM public.procrastinate_workers WHERE id=%s FOR UPDATE""",
                (STALLED_SECONDS, row["worker_id"])).fetchone()
            if worker and worker["live"]:
                return False
        if row["task_name"] == MAINTENANCE_TASK:
            # Future periodic ticks continue independently; do not retry a stale
            # maintenance job forever or let it hold an execution lock.
            db.execute("SELECT public.procrastinate_finish_job_v1(%s,'failed',false)", (job_id,))
            return True
        import_id = row["args"].get("import_id")
        if not isinstance(import_id, str) or not IMPORT_ID.fullmatch(import_id):
            db.execute("SELECT public.procrastinate_finish_job_v1(%s,'failed',false)", (job_id,))
            return True
        state = db.execute("""SELECT i.status,(d.import_id IS NOT NULL) AS dead
            FROM aster_archive.imports i LEFT JOIN aster_archive.import_dead_letters d USING(import_id)
            WHERE i.import_id=%s""", (import_id,)).fetchone()
        if state and state["status"] in ("complete", "complete_with_errors"):
            db.execute("SELECT public.procrastinate_finish_job_v1(%s,'succeeded',false)", (job_id,))
        elif (not state or state["dead"] or row["abort_requested"]
              or row["attempts"] + 1 >= MAX_ATTEMPTS):
            _record_dead_letter(db, import_id, job_id, "stalled_attempts_exhausted", row["attempts"] + 1)
            db.execute("SELECT public.procrastinate_finish_job_v1(%s,'failed',false)", (job_id,))
        else:
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=30 * (row["attempts"] + 1))
            db.execute("SELECT public.procrastinate_retry_job_v2(%s,%s,NULL,NULL,NULL)", (job_id, retry_at))
        return True


async def recover_stalled(app, dsn: str) -> int:
    recovered = 0
    for queue, task_name in ((IMPORT_QUEUE, IMPORT_TASK), (MAINTENANCE_QUEUE, MAINTENANCE_TASK)):
        candidates = await app.job_manager.get_stalled_jobs(
            queue=queue, task_name=task_name, seconds_since_heartbeat=STALLED_SECONDS,
        )
        for job in list(candidates)[:100]:
            recovered += int(await asyncio.to_thread(_recover_stalled, dsn, job.id))
    return recovered


def build_app(dsn: str, archive_root: str | Path):
    _version_check()
    archive_root = Path(archive_root)
    if not archive_root.is_absolute():
        raise ValueError("absolute archive root required")
    connector = procrastinate.PsycopgConnector(
        conninfo=dsn, min_size=1, max_size=6,
        kwargs={"connect_timeout": 10, "options": _CONNECT_OPTIONS},
    )
    app = procrastinate.App(connector=connector, worker_defaults={
        "queues": [IMPORT_QUEUE, MAINTENANCE_QUEUE],
        "concurrency": 2,
        "delete_jobs": "never",
        "update_heartbeat_interval": HEARTBEAT_SECONDS,
        "stalled_worker_timeout": STALLED_SECONDS,
        "shutdown_graceful_timeout": 30,
    })
    retry = procrastinate.RetryStrategy(
        max_attempts=MAX_ATTEMPTS - 1, wait=30, linear_wait=30,
        retry_exceptions=(TransientImportFailure,),
    )

    @app.task(name=IMPORT_TASK, queue=IMPORT_QUEUE, lock=IMPORT_LOCK,
              retry=retry, pass_context=True)
    async def import_snapshot(context, *, import_id: str):
        if not isinstance(import_id, str) or not IMPORT_ID.fullmatch(import_id):
            raise PermanentImportFailure("ARCHIVE_IMPORT_INVALID_ID")
        try:
            exit_code = await _import_subprocess(dsn, import_id, archive_root)
        except asyncio.CancelledError:
            if context.abort_reason() == AbortReason.SHUTDOWN:
                # The child has been stopped before retrying. A normal service
                # restart must not strand an acknowledged import in aborted.
                exit_code = 21
            else:
                await asyncio.to_thread(_dead_letter, dsn, import_id, context.job.id,
                                        "import_cancelled", context.job.attempts + 1)
                raise
        except (OSError, asyncio.TimeoutError):
            exit_code = 21
        if exit_code == 0:
            return {"import_id": import_id, "status": "finished"}
        transient = exit_code == 21 or exit_code < 0
        exhausted = context.job.attempts + 1 >= MAX_ATTEMPTS
        if not transient or exhausted:
            reason = ("transient_attempts_exhausted" if transient
                      else "invalid_source" if exit_code == 20 else "import_unexpected_failure")
            try:
                await asyncio.to_thread(_dead_letter, dsn, import_id, context.job.id,
                                        reason, context.job.attempts + 1)
            except Exception:
                raise TransientImportFailure("ARCHIVE_IMPORT_RECEIPT_UNAVAILABLE") from None
        if transient:
            raise TransientImportFailure("ARCHIVE_IMPORT_TRANSIENT")
        raise PermanentImportFailure("ARCHIVE_IMPORT_PERMANENT")

    @app.periodic(cron="*/1 * * * *", periodic_id="archive-heartbeat-recovery-v1")
    @app.task(name=MAINTENANCE_TASK, queue=MAINTENANCE_QUEUE, pass_context=True)
    async def maintain(context, *, timestamp: int):
        try:
            delivered = await asyncio.to_thread(dispatch_pending, dsn, archive_root)
            recovered = await recover_stalled(context.app, dsn)
            return {"dispatched": delivered, "recovered": recovered}
        except Exception:
            raise QueueError("ARCHIVE_QUEUE_MAINTENANCE_UNAVAILABLE") from None

    return app


def dispatch_pending(dsn: str, archive_root: str | Path) -> int:
    """Atomically enqueue and acknowledge a bounded batch of outbox rows."""
    app = build_app(dsn, archive_root)
    try:
        with _connect(dsn) as db:
            rows = db.execute("""SELECT o.import_id FROM aster_archive.import_outbox o
                JOIN aster_archive.imports i USING(import_id)
                WHERE o.dispatched_job_id IS NULL
                ORDER BY i.created_at,o.import_id
                LIMIT 100 FOR UPDATE OF o SKIP LOCKED""").fetchall()
            for row in rows:
                # The pinned connector uses nullcontext(connection): it does not
                # commit, replace or close this transaction before our ack.
                job_id = app.tasks[IMPORT_TASK].configure(connection=db).defer(import_id=row["import_id"])
                db.execute("""UPDATE aster_archive.import_outbox
                    SET dispatched_job_id=%s,dispatched_at=now() WHERE import_id=%s""",
                    (job_id, row["import_id"]))
            return len(rows)
    finally:
        app.close()


def run_worker(dsn: str, archive_root: str | Path) -> None:
    """Serve continuously; at most one import plus an independent maintenance slot."""
    app = build_app(dsn, archive_root)

    async def serve():
        async with app.open_async():
            await recover_stalled(app, dsn)
            await asyncio.to_thread(dispatch_pending, dsn, archive_root)
            await app.run_worker_async()
    try:
        asyncio.run(serve())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise QueueError("ARCHIVE_QUEUE_WORKER_UNAVAILABLE") from None


if __name__ == "__main__":
    # This private child entrypoint is invoked only with an opaque ID and a
    # configured archive directory. Credentials remain out of argv and output.
    if len(sys.argv) == 4 and sys.argv[1] == "_import":
        try:
            _arm_worker_parent()
        except QueueError:
            sys.exit(21)
        sys.exit(_run_import_once(os.environ.get("ASTER_ARCHIVE_DATABASE_URL", ""), sys.argv[2], sys.argv[3]))
    raise SystemExit("Use the archive CLI to start a worker.")
