"""Portable, bounded filesystem enumeration for MCP M1."""
from __future__ import annotations
import os
import stat
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

MAX_ENTRIES = 200


class DirectoryAccessDenied(PermissionError):
    """A requested path resolved outside the caller's explicit listing boundary."""


def list_directory(path: str = ".", *, root: str | None = None, limit: int = 100,
                   base: str | None = None, names_only: bool = False) -> dict:
    """Enumerate at most limit + 1 entries, with an unknown total if truncated.

    Relative paths use base, or root when it is supplied, or the current working
    directory for legacy unconfined callers. Sorting is within the bounded
    returned sample, never a scan/sort of an entire large directory.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_ENTRIES:
        raise ValueError("limit must be between 1 and 200")
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("invalid path")
    boundary = Path(root).resolve(strict=True) if root is not None else None
    anchor = Path(base).resolve(strict=True) if base is not None else boundary
    requested = Path(path)
    if anchor is None:
        target = requested.expanduser().resolve(strict=True)
    else:
        target = (anchor / requested).resolve(strict=True)
    if root is not None:
        if not target.is_relative_to(boundary):
            raise DirectoryAccessDenied("path outside allowed root")
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    result = []
    with os.scandir(target) as iterator:
        entries = list(islice(iterator, limit + 1))
    truncated = len(entries) > limit
    for entry in sorted(entries[:limit], key=lambda e: e.name):
        if names_only:
            result.append(entry.name)
            continue
        info = entry.stat(follow_symlinks=False)
        result.append({
            "name": entry.name,
            "mode": stat.filemode(info.st_mode),
            "size": info.st_size,
            "modified_utc": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
            "is_directory": stat.S_ISDIR(info.st_mode),
            "is_symlink": stat.S_ISLNK(info.st_mode),
            "uid": getattr(info, "st_uid", None),
            "gid": getattr(info, "st_gid", None),
        })
    return {"path": str(target), "entries": result,
            "total": None if truncated else len(entries),
            "total_is_exact": not truncated, "truncated": truncated, "limit": limit,
            "note": "Includes hidden entries; '.' and '..' are not physical directory entries. "
                    "Only the bounded returned sample is sorted. Total is unknown when truncated."}
