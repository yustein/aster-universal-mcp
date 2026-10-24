#!/usr/bin/env python3
"""Freeze reviewed source and build the Windows source archive with stdlib only.

Run this only after reviewing the intended changes. The resulting hash identifies
the source bytes; authenticity comes from the reviewed release commit/channel.
Acceptance evidence is produced separately, so testing never changes the archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", "dist", "build", "artifacts"}


def source_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise ValueError("source root must not be a link")
    result = []
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if name not in EXCLUDED_DIRS)
        for name in folders + files:
            if (Path(directory) / name).is_symlink():
                raise ValueError("source links are not allowed")
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative in {"candidate-manifest.json", ".coverage"} or path.suffix in {".pyc", ".pyo"}:
                continue
            result.append(path)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True,
                        help="Outside source or beneath an excluded artifacts directory")
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--version", default="0.1.0b1")
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", args.base_commit):
        raise ValueError("base commit must be a complete Git SHA")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", args.version):
        raise ValueError("invalid release version")
    if output == root or (root in output.parents and not any(
            part in EXCLUDED_DIRS for part in output.relative_to(root).parts)):
        raise ValueError("output must not become part of the frozen source inventory")
    files = source_files(root)
    if not files:
        raise ValueError("empty source")
    contents = {p.relative_to(root).as_posix(): p.read_bytes() for p in files}
    manifest = {"schema_version": 1, "base_commit": args.base_commit,
                "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in contents.items()]}
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (root / "candidate-manifest.json").write_bytes(manifest_bytes)
    contents["candidate-manifest.json"] = manifest_bytes
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"aster-universal-mcp-{args.version}-windows.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in sorted(contents):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, contents[name], compresslevel=9)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None or set(bundle.namelist()) != set(contents):
            raise ValueError("archive integrity check failed")
        for name, data in contents.items():
            if bundle.read(name) != data:
                raise ValueError("archive content mismatch")
    manifest_path = output / "candidate-manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    candidate_id = hashlib.sha256(manifest_bytes).hexdigest()
    (output / "SHA256SUMS.txt").write_text(
        f"{archive_hash}  {archive.name}\n{candidate_id}  {manifest_path.name}\n", encoding="utf-8")
    record = {"version": args.version, "candidate_id": candidate_id,
              "base_commit": args.base_commit, "source_file_count": len(files),
              "archive": archive.name, "archive_sha256": archive_hash,
              "archive_bytes": archive.stat().st_size,
              "archive_layout": "source files at archive root",
              "acceptance_status": "not established by the packaging operation"}
    (output / "build-record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
