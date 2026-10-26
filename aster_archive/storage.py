"""Small free-space reserve checks for archive acquisition and ingestion.

These checks measure the filesystem backing the protected archive root. They
are headroom checks, not a filesystem quota or a reservation against other
writers. A database on another filesystem needs its own capacity monitoring.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import shutil


DEFAULT_MIN_FREE_BYTES = 1024**3
MAX_MIN_FREE_BYTES = 1024**5


class ArchiveStorageError(OSError):
    """Retryable capacity failure; never classify this as invalid export data."""


def minimum_free_bytes(*, environ=None) -> int:
    values = os.environ if environ is None else environ
    raw = values.get("ASTER_ARCHIVE_MIN_FREE_BYTES", str(DEFAULT_MIN_FREE_BYTES))
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,16}", raw.strip()):
        raise ValueError("ASTER_ARCHIVE_MIN_FREE_BYTES must be a positive integer no greater than 1 PiB")
    value = int(raw)
    if not 1 <= value <= MAX_MIN_FREE_BYTES:
        raise ValueError("ASTER_ARCHIVE_MIN_FREE_BYTES must be a positive integer no greater than 1 PiB")
    return value


def require_storage_reserve(archive_root: Path, *, additional_bytes: int = 0) -> None:
    """Require reserve plus an imminent copy's additional bytes before writing."""
    if type(additional_bytes) is not int or not 0 <= additional_bytes <= 2**63 - 1:
        raise ValueError("additional storage bytes must be a bounded nonnegative integer")
    reserve = minimum_free_bytes()
    root = Path(archive_root).resolve(strict=True)
    if not root.is_dir():
        raise ArchiveStorageError(errno.ENOTDIR, "archive storage root is not a directory")
    available = shutil.disk_usage(root).free
    if available < reserve + additional_bytes:
        raise ArchiveStorageError(errno.ENOSPC, "archive free-space reserve would be crossed")
