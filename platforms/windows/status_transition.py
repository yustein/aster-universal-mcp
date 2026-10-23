"""Serialized, fail-closed Windows job status transitions.

Cross-process lock is a private per-job lock file. Terminal states cannot be
overwritten by late worker completion, cancellation, or recovery.
"""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
import msvcrt

# Readers use normal Python file opens, which may temporarily block replacing
# status.json on Windows (WinError 5/32). Keep the per-job lock during a short,
# bounded retry; any lasting denial must still fail closed.
_REPLACE_RETRY_SECONDS = 3.0
_REPLACE_RETRY_WINERRORS = frozenset({5, 32})


def _replace_status(temp: Path, path: Path):
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(temp, path)
            return
        except OSError as exc:
            # Don't reinterpret arbitrary PermissionError (or Unix errno 5)
            # as a transient Windows sharing violation.
            if getattr(exc, "winerror", None) not in _REPLACE_RETRY_WINERRORS:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(0.025, max(0, deadline - time.monotonic())))


TERMINAL = frozenset({"completed", "failed", "timeout", "cancelled", "interrupted_by_reboot", "cancellation_interrupted_by_reboot", "cancellation_worker_missing", "worker_termination_unverified"})
ACTIVE = frozenset({"queued", "running"})

@contextmanager
def locked(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "status.lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 10
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("job status lock unavailable")
                time.sleep(0.02)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

def transition(folder: Path, status: str, **fields):
    with locked(folder):
        path = folder / "status.json"
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if previous.get("status") in TERMINAL:
            return previous, False
        if previous.get("status") == "cancelling" and status not in ("cancelled", "cancel_failed"):
            return previous, False
        if status == "cancelling" and previous.get("status") not in ACTIVE:
            return previous, False
        updated = dict(previous)
        updated.update(fields)
        updated["status"] = status
        temp = folder / ("status-" + str(os.getpid()) + ".tmp")
        try:
            temp.write_text(json.dumps(updated), encoding="utf-8")
            _replace_status(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return updated, True
