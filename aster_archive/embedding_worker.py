"""Local-only bounded embedding worker and public-model preparation CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import threading

from .access import ArchiveScope
from .embedding_store import EmbeddingStore
from .embeddings import LocalE5Encoder, generation_descriptor, generation_id, prepare_model
from .storage import ArchiveStorageError, minimum_free_bytes, require_storage_reserve
from .store import PostgresArchive, _identity


def run_batch(embeddings: EmbeddingStore, scope: ArchiveScope, encoder,
              archive_root: Path, *, batch_size: int = 4) -> dict:
    """One durable batch; model work never runs inside a database transaction."""
    require_storage_reserve(archive_root)
    identity = encoder.generation_id
    jobs = embeddings.claim(scope, identity, limit=batch_size)
    if not jobs:
        return {"claimed": 0, "completed": 0, "generation_id": identity}
    try:
        values = encoder.embed_passages([job["text"] for job in jobs])
        require_storage_reserve(archive_root)
        completed = embeddings.complete(scope, identity, jobs, values)
    except ArchiveStorageError:
        embeddings.release(scope, identity, jobs)
        raise
    except (KeyboardInterrupt, SystemExit):
        embeddings.release(scope, identity, jobs)
        raise
    except Exception:
        embeddings.fail(scope, identity, jobs)
        raise
    return {"claimed": len(jobs), "completed": completed, "generation_id": identity}


def run_worker(embeddings: EmbeddingStore, scope: ArchiveScope, encoder,
               archive_root: Path, *, batch_size: int = 4, once: bool = False,
               poll_seconds: int = 60, stop: threading.Event | None = None) -> dict:
    if type(poll_seconds) is not int or not 1 <= poll_seconds <= 3600:
        raise ValueError("invalid embedding poll interval")
    minimum_free_bytes()
    archive_root = Path(archive_root).resolve(strict=True)
    stop = stop or threading.Event()
    identity = embeddings.register_generation(encoder.descriptor)
    if identity != encoder.generation_id:
        raise ValueError("embedding generation mismatch")
    total = 0
    while not stop.is_set():
        require_storage_reserve(archive_root)
        seeded = embeddings.enqueue(scope, identity)
        embeddings.sync_derivations(scope, identity)
        while not stop.is_set():
            batch = run_batch(embeddings, scope, encoder, archive_root, batch_size=batch_size)
            total += batch["completed"]
            if once or not batch["claimed"]:
                break
        embeddings.sync_derivations(scope, identity)
        if once:
            return {"status": "batch_complete", "seeded": seeded, "completed": total,
                    "coverage": embeddings.coverage(scope, identity)}
        if seeded == 2000:
            continue
        stop.wait(poll_seconds)
    return {"status": "stopped", "completed": total, "generation_id": identity}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Local source-backed archive embeddings")
    commands = result.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-model", help="fetch pinned public artifacts; never sends corpus")
    prepare.add_argument("--model-dir", type=Path, required=True)
    commands.add_parser("migrate", help="apply additive embeddings migration after operator installs pgvector")
    for name in ("run", "status", "retry-failed"):
        command = commands.add_parser(name)
        command.add_argument("--owner", required=True)
        command.add_argument("--source", required=True)
        if name == "run":
            command.add_argument("--model-dir", type=Path, required=True)
            command.add_argument("--archive-root", type=Path, required=True)
            command.add_argument("--threads", type=int, default=2)
            command.add_argument("--batch-size", type=int, default=4)
            command.add_argument("--once", action="store_true", help="process one bounded batch and exit")
            command.add_argument("--poll-seconds", type=int, default=60)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare-model":
            result = prepare_model(args.model_dir)
        else:
            embeddings = EmbeddingStore(PostgresArchive(os.environ.get("ASTER_ARCHIVE_DATABASE_URL", "")))
            if args.command == "migrate":
                embeddings.migrate()
                result = {"status": "embedding_schema_ready"}
            else:
                scope = ArchiveScope("local-embedding-worker", _identity(args.owner),
                                     (_identity(args.source),), None)
                identity = generation_id(generation_descriptor())
                if args.command == "status":
                    result = embeddings.coverage(scope, identity)
                elif args.command == "retry-failed":
                    result = {"retried": embeddings.retry_failed(scope, identity)}
                else:
                    encoder = LocalE5Encoder(args.model_dir, threads=args.threads, batch_size=args.batch_size)
                    stop = threading.Event()
                    previous = {}
                    for signum in (signal.SIGTERM, signal.SIGINT):
                        previous[signum] = signal.signal(signum, lambda *_: stop.set())
                    try:
                        result = run_worker(embeddings, scope, encoder, args.archive_root,
                                            batch_size=args.batch_size, once=args.once,
                                            poll_seconds=args.poll_seconds, stop=stop)
                    finally:
                        for signum, handler in previous.items():
                            signal.signal(signum, handler)
        print(json.dumps(result, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Never print SQL, DSNs, corpus text, vectors, or raw model exceptions.
        print(json.dumps({"status": "failed", "error_code": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
