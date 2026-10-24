#!/usr/bin/env python3
"""Build a reproducible, mode-preserving Linux source artifact from an exact clean Git commit."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--version", default="0.1.0b3")
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
        raise ValueError("full source commit required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", args.version):
        raise ValueError("invalid version")
    if output == root or root in output.parents:
        raise ValueError("output must be outside source")
    if git(root, "rev-parse", "HEAD").decode().strip() != args.source_commit:
        raise ValueError("source commit is not checkout HEAD")
    if git(root, "status", "--porcelain").strip():
        raise ValueError("source checkout must be clean, including untracked files")
    tree = git(root, "rev-parse", args.source_commit + "^{tree}").decode().strip()
    files = []
    contents = {}
    for entry in git(root, "ls-tree", "-r", "-z", args.source_commit).split(b"\0"):
        if not entry:
            continue
        metadata, rawname = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        name = rawname.decode("utf-8")
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("only regular source files are supported")
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or name == "candidate-manifest.json":
            raise ValueError("invalid or circular source entry")
        data = git(root, "cat-file", "blob", blob)
        if (root / name).read_bytes() != data:
            raise ValueError("checkout bytes differ from committed source")
        contents[name] = data
        files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                      "bytes": len(data), "mode": int(mode[-3:], 8)})
    files.sort(key=lambda value: value["path"])
    manifest = {"schema_version": 2, "platform": "linux", "version": args.version,
                "source_commit": args.source_commit, "source_tree": tree,
                "files": files}
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
    contents["candidate-manifest.json"] = manifest_bytes
    modes = {item["path"]: item["mode"] for item in files}
    modes["candidate-manifest.json"] = 0o644
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"aster-universal-mcp-{args.version}-linux.tar.gz"
    if archive.exists():
        raise ValueError("refuse to overwrite a candidate artifact")
    with archive.open("xb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=9) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as bundle:
                for name in sorted(contents):
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode = len(contents[name]), modes[name]
                    entry.uid = entry.gid = entry.mtime = 0
                    bundle.addfile(entry, io.BytesIO(contents[name]))
    with tarfile.open(archive, "r:gz") as bundle:
        if set(bundle.getnames()) != set(contents):
            raise ValueError("archive inventory mismatch")
        for member in bundle.getmembers():
            if not member.isfile() or member.mode != modes[member.name] or bundle.extractfile(member).read() != contents[member.name]:
                raise ValueError("archive verification failed")
    candidate = hashlib.sha256(manifest_bytes).hexdigest()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "candidate-manifest.json").write_bytes(manifest_bytes)
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n{candidate}  candidate-manifest.json\n")
    record = {"version": args.version, "source_commit": args.source_commit, "source_tree": tree,
              "candidate_id": candidate, "archive": archive.name, "archive_sha256": digest,
              "archive_bytes": archive.stat().st_size, "source_file_count": len(files),
              "archive_layout": "source files at archive root; POSIX regular-file modes preserved",
              "acceptance_status": "not established by packaging"}
    (output / "build-record.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))

if __name__ == "__main__":
    main()

