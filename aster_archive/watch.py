"""Discover completed snapshots, register durable imports, then dispatch work."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import stat
import threading

from .source import SourceError, prepare_source
from .store import PostgresArchive


class InboxWatcher:
    def __init__(self, store: PostgresArchive, *, inbox: Path, archive_root: Path,
                 owner_id: str, source_id: str):
        self.store = store
        self.inbox = Path(inbox).resolve(strict=True)
        self.archive_root = Path(archive_root).resolve()
        if not self.inbox.is_dir():
            raise ValueError("import inbox must be a directory")
        self.owner_id, self.source_id = owner_id, source_id
        self._previous: dict[str, tuple] = {}

    def scan_once(self) -> list[dict]:
        current, events = {}, []
        for path in sorted(self.inbox.iterdir()):
            if path.suffix.lower() not in (".zip", ".json"):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            name = str(path)
            current[name] = fingerprint
            if self._previous.get(name) != fingerprint:
                continue
            # Include inode/ctime in the receipt key so replacing a filename
            # with equal size and preserved mtime still receives a fresh hash.
            receipt_path = f"{name}#identity={info.st_dev}:{info.st_ino}:{info.st_ctime_ns}"
            values = (self.owner_id, self.source_id, receipt_path, info.st_size, info.st_mtime_ns)
            if self.store.inbox_seen(*values):
                continue
            try:
                source = prepare_source(path, self.archive_root)
                import_id = self.store.register_import(source, self.owner_id, self.source_id)
                self.store.record_inbox(*values, import_id=import_id)
                events.append({"event": "import_registered", "import_id": import_id})
            except SourceError:
                self.store.record_inbox(*values, error_code="invalid_source")
                events.append({"event": "source_quarantined", "error_code": "invalid_source"})
            except OSError:
                # Do not make a transient I/O failure a permanent skip receipt.
                events.append({"event": "source_retry_pending", "error_code": "source_io_error"})
        self._previous = current
        return events


def run_watch(store: PostgresArchive, *, inbox: Path, archive_root: Path,
              owner_id: str, source_id: str, poll_seconds: int = 60) -> None:
    from .queue import dispatch_pending

    if not 5 <= poll_seconds <= 3600:
        raise ValueError("poll interval must be between 5 and 3600 seconds")
    watcher = InboxWatcher(store, inbox=inbox, archive_root=archive_root,
                           owner_id=owner_id, source_id=source_id)
    stop = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())
    while not stop.is_set():
        try:
            for event in watcher.scan_once():
                print(json.dumps(event), flush=True)
            count = dispatch_pending(store.dsn, archive_root)
            if count:
                print(json.dumps({"event": "imports_dispatched", "count": count}), flush=True)
        except Exception as error:
            print(json.dumps({"event": "watch_retry_pending", "error_code": type(error).__name__}), flush=True)
        stop.wait(poll_seconds)
