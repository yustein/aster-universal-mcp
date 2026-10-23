"""Portable, bounded filesystem enumeration for MCP M1."""
from __future__ import annotations
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 200

def list_directory(path: str = ".", *, root: str | None = None, limit: int = 100) -> dict:
    """Return ls -la-equivalent metadata, including hidden entries."""
    if not 1 <= limit <= MAX_ENTRIES:
        raise ValueError("limit must be between 1 and 200")
    if not path or "\x00" in path:
        raise ValueError("invalid path")
    if root is not None:
        base = Path(root).resolve(strict=True)
        requested = Path(path)
        target = (base / requested).resolve(strict=True) if not requested.is_absolute() else requested.resolve(strict=True)
        if not target.is_relative_to(base):
            raise PermissionError("path outside allowed root")
    else:
        target = Path(path).expanduser().resolve(strict=True)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    result = []
    with os.scandir(target) as iterator:
        entries = sorted(iterator, key=lambda e: e.name)
    truncated = len(entries) > limit
    for entry in entries[:limit]:
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
    return {"path": str(target), "entries": result, "total": len(entries), "truncated": truncated,
            "note": "Includes hidden entries; '.' and '..' are not physical directory entries."}
