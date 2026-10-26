"""Stream private archive/config and a consistent SQLite snapshot to stdout.

Called by scripts/archive-backup.sh after the import writer has stopped.
The stdout stream is binary and must be redirected to a private backup file.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile


def main() -> None:
    archive = Path(os.environ["ASTER_ARCHIVE_ROOT"])
    auth = Path(os.environ["ASTER_ARCHIVE_OAUTH_DB"])
    clients = Path(os.environ["ASTER_ARCHIVE_CLIENTS"])
    if not archive.is_dir() or not auth.is_file() or not clients.is_file():
        raise SystemExit("Archive root, OAuth database, and client ACL must all exist")
    with tempfile.TemporaryDirectory(prefix="archive-auth-backup-") as temporary:
        snapshot = Path(temporary) / "oauth.sqlite3"
        with sqlite3.connect(auth.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(snapshot) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("OAuth SQLite backup failed integrity check")
        snapshot.chmod(0o600)
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz", dereference=False) as output:
            output.add(archive, arcname="archive", recursive=True)
            output.add(snapshot, arcname="auth/oauth.sqlite3", recursive=False)
            output.add(clients, arcname="config/clients.json", recursive=False)


if __name__ == "__main__":
    main()
