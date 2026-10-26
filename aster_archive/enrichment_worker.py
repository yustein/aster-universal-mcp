"""Supervised local knowledge/Cognee workers using the durable component jobs.

Each loop handles one bounded batch, then waits. Logs contain only component
names, counts and fixed error codes: never historical text, SQL or credentials.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import sys
import threading

import psycopg

from .cognee_bridge import CogneeConfig, CogneeUnavailable, process_pending
from .knowledge import derive_pending
from .storage import ArchiveStorageError, minimum_free_bytes
from .store import PostgresArchive, _identity


class _ShutdownRequested(KeyboardInterrupt):
    """Unwind database contexts and the isolated child on supervised shutdown."""


def _emit(event: dict) -> None:
    print(json.dumps(event, allow_nan=False), flush=True)


def _error_code(error: Exception) -> str:
    # Do not print exception messages, arbitrary type names, paths or arguments.
    if isinstance(error, ArchiveStorageError):
        return 'STORAGE_RESERVE_UNAVAILABLE'
    if isinstance(error, CogneeUnavailable):
        return 'LOCAL_COGNEE_UNAVAILABLE'
    if isinstance(error, psycopg.OperationalError):
        return 'DATABASE_UNAVAILABLE'
    if isinstance(error, psycopg.Error):
        return 'DATABASE_OPERATION_FAILED'
    if isinstance(error, OSError):
        return 'LOCAL_IO_UNAVAILABLE'
    if isinstance(error, ValueError):
        return 'INVALID_CONFIGURATION_OR_RESULT'
    return 'ENRICHMENT_OPERATION_FAILED'


def _backlog_enabled() -> bool:
    value = os.environ.get('ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG', '0')
    if value not in ('0', '1'):
        raise ValueError('invalid Cognee backlog setting')
    return value == '1'


@contextmanager
def _shutdown_signals(stop: threading.Event):
    previous = {}

    def shutdown(signum, frame):
        # A second termination signal must not interrupt child cleanup.
        if not stop.is_set():
            stop.set()
            raise _ShutdownRequested()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, shutdown)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_worker(component: str, store: PostgresArchive, owner_id: str,
               source_id: str, archive_root: Path, *, batch_size: int | None = None,
               poll_seconds: int = 60, once: bool = False, backlog: bool = False,
               config: CogneeConfig | None = None, stop: threading.Event | None = None,
               emit=None) -> dict:
    """Resume existing native jobs; sample mode never enables backlog itself.

    Startup errors propagate before any job can be claimed. Operational failures
    wait for the next poll; per-job retry limits remain owned by each component.
    SIGTERM in the CLI interrupts the active child and releases the source lock.
    A running job retains its durable attempt/checkpoint for restart recovery.
    """
    if component not in ('knowledge', 'cognee'):
        raise ValueError('unknown enrichment component')
    _identity(owner_id); _identity(source_id)
    if batch_size is None:
        batch_size = 10 if component == 'knowledge' else 2
    maximum = 100 if component == 'knowledge' else 20
    if type(batch_size) is not int or not 1 <= batch_size <= maximum:
        raise ValueError('invalid enrichment batch size')
    if type(poll_seconds) is not int or not 1 <= poll_seconds <= 3600:
        raise ValueError('invalid enrichment poll interval')
    if type(once) is not bool or type(backlog) is not bool or (backlog and component != 'cognee'):
        raise ValueError('invalid enrichment mode')
    minimum_free_bytes()
    archive_root = Path(archive_root)
    if not archive_root.is_absolute() or not archive_root.is_dir():
        raise ValueError('absolute existing archive directory required')
    archive_root = archive_root.resolve(strict=True)
    if component == 'cognee':
        config = config or CogneeConfig.from_env()
        config.validate()
    stop = stop if stop is not None else threading.Event()
    emit = emit or _emit
    mode = 'backlog' if backlog else ('bounded_sample' if component == 'cognee' else 'extractive')
    base = {'component': component, 'mode': mode}
    completed, failed, errors = 0, 0, 0
    emit(base | {'event': 'started', 'batch_size': batch_size, 'poll_seconds': poll_seconds})
    try:
        while not stop.is_set():
            try:
                if component == 'knowledge':
                    result = derive_pending(store, owner_id, source_id, archive_root, limit=batch_size)
                else:
                    result = process_pending(store, owner_id, source_id, archive_root,
                                             config, limit=batch_size, backlog=backlog)
                counts = {key: result[key] for key in ('completed', 'failed')}
                if (any(type(value) is not int or not 0 <= value <= batch_size for value in counts.values())
                        or sum(counts.values()) > batch_size):
                    raise ValueError('invalid component result counts')
            except Exception as error:
                errors += 1
                emit(base | {'event': 'batch_error', 'error_code': _error_code(error)})
                if once:
                    return base | {'status': 'failed', 'completed': completed,
                                   'failed': failed, 'batch_errors': errors}
            else:
                completed += counts['completed']; failed += counts['failed']
                emit(base | {'event': 'batch_complete' if sum(counts.values()) else 'idle', **counts})
                if once:
                    return base | {'status': 'batch_complete', 'completed': completed,
                                   'failed': failed, 'batch_errors': errors}
            # Idle sample workers keep their service alive but do not seed more
            # than SAMPLE_LIMIT. New exports never implicitly unlock backlog.
            stop.wait(poll_seconds)
    except _ShutdownRequested:
        stop.set()
    return base | {'status': 'stopped', 'completed': completed,
                   'failed': failed, 'batch_errors': errors}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='component', required=True)
    for component in ('knowledge', 'cognee'):
        command = commands.add_parser(component)
        command.add_argument('--owner', required=True)
        command.add_argument('--source', required=True)
        command.add_argument('--archive-root', type=Path, required=True)
        command.add_argument('--batch-size', type=int, default=10 if component == 'knowledge' else 2)
        command.add_argument('--poll-seconds', type=int, default=60)
        command.add_argument('--once', action='store_true', help='process one bounded batch and exit')
        if component == 'cognee':
            command.add_argument('--enable-backlog', action='store_true',
                                 help='explicit expansion; also enabled by ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG=1')
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    stop = threading.Event()
    try:
        backlog = (_backlog_enabled() or args.enable_backlog) if args.component == 'cognee' else False
        with _shutdown_signals(stop):
            result = run_worker(args.component,
                PostgresArchive(os.environ.get('ASTER_ARCHIVE_DATABASE_URL', '')),
                args.owner, args.source, args.archive_root, batch_size=args.batch_size,
                poll_seconds=args.poll_seconds, once=args.once,
                backlog=backlog, stop=stop)
        _emit(result)
        return 1 if result['status'] == 'failed' else 0
    except _ShutdownRequested:
        _emit({'component': args.component, 'status': 'stopped'})
        return 0
    except Exception as error:
        print(json.dumps({'component': args.component, 'status': 'startup_failed',
                          'error_code': _error_code(error)}), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
