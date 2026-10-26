#!/usr/bin/env python3
"""Restore a checked backup to labelled disposable targets; never replace live data.

Run as root on the archive host. No services are stopped or restarted.
Failures preserve the private checkpoint and test targets for inspection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

LABEL = "io.aster.archive.restore-token"
RESERVE = 10 * 1024**3
DEFAULT_MAX_MEMBERS = 2_000_000
MAX_MEMBER_BUDGET = 50_000_000
REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy/archive"
PROBE = DEPLOY / "recovery_probe.py"
COMPOSE = ["docker", "compose", "--project-directory", str(DEPLOY),
           "--env-file", str(DEPLOY / ".env"), "-f", str(DEPLOY / "compose.yaml")]
EXTENSION_DDL = {
    "vector": b"CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;",
    "plpgsql": b"CREATE EXTENSION IF NOT EXISTS plpgsql WITH SCHEMA pg_catalog;",
}


def validate_member_budget(value):
    if type(value) is not int or not 1 <= value <= MAX_MEMBER_BUDGET:
        raise ValueError("restore member budget must be between 1 and 50000000")
    return value


def prepare_restore_list(contents):
    """Keep ordinary metadata; handle only allowlisted extensions as admin.

    Both CREATE EXTENSION and its COMMENT must be omitted from the ingest-role
    restore. Precreating an admin-owned extension alone does not permit the
    ingest role to execute COMMENT ON EXTENSION.
    """
    extensions, lines = set(), []
    for line in contents.splitlines(keepends=True):
        item = re.match(r"^\d+;\s+\d+\s+\d+\s+(.*)$", line)
        description = item[1] if item else ""
        prefix = next((value for value in ("EXTENSION - ", "COMMENT - EXTENSION ")
                       if description.startswith(value)), None)
        if prefix is None:
            lines.append(line)
            continue
        name = description[len(prefix):].split()[0]
        if name not in EXTENSION_DDL:
            raise ValueError("backup contains an unsupported privileged extension")
        extensions.add(name)
    return "".join(lines), sorted(extensions)


def save(path, data):
    temporary = path.with_suffix(".partial")
    with temporary.open("w") as stream:
        json.dump(data, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def command(argv, *, log, source=None, capture=False, text=None, timeout=7200):
    stdin = source if source is not None else (None if text is not None else subprocess.DEVNULL)
    result = subprocess.run(argv, stdin=stdin, input=text,
                            stdout=subprocess.PIPE if capture else log,
                            stderr=log, timeout=timeout, check=True)
    return result.stdout if capture else None


def checked_backup(path):
    path = path.resolve(strict=True)
    expected = {"database.dump", "files.tar.gz", "deployment.env"}
    manifest = path / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError("missing checksum manifest")
    rows = {}
    for line in manifest.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (database\.dump|files\.tar\.gz|deployment\.env)", line)
        if match is None or match[2] in rows:
            raise ValueError("unexpected checksum manifest")
        rows[match[2]] = match[1]
    if rows.keys() != expected:
        raise ValueError("incomplete checksum manifest")
    for name, digest in rows.items():
        item = path / name
        if not item.is_file() or item.is_symlink():
            raise ValueError("backup member must be a regular file")
        with item.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("backup checksum mismatch")
    return path


def validate_state(state):
    token = state["token"]
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid restore token")
    database = "aster_archive_restore_" + token[:24]
    if state["database"] != database:
        raise ValueError("invalid restore database")
    if state["volumes"] != {kind: database + "_" + kind for kind in ("objects", "auth", "config")}:
        raise ValueError("invalid restore volumes")
    return "aster-archive-restore:" + token


def cleanup(state, state_path, log):
    marker = validate_state(state)
    query = b"SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:'target';"
    comment = command(COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", "postgres",
                      "-At", "-v", "ON_ERROR_STOP=1", "-v", "target=" + state["database"]],
                      log=log, text=query, capture=True).decode().strip()
    if comment:
        if comment != marker:
            raise ValueError("restore database marker mismatch")
        command(COMPOSE + ["exec", "-T", "db", "dropdb", "-U", "postgres", state["database"]], log=log)
    else:
        exists = command(COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", "postgres",
                         "-At", "-v", "ON_ERROR_STOP=1", "-v", "target=" + state["database"]],
                         log=log, text=b"SELECT count(*) FROM pg_database WHERE datname=:'target';", capture=True)
        if exists.strip() != b"0":
            raise ValueError("unmarked database requires manual review")
    for volume in state["volumes"].values():
        inspected = subprocess.run(["docker", "volume", "inspect", volume], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=log, timeout=30)
        if inspected.returncode != 0:
            # Distinguish a missing exact target from a broken daemon.
            known = command(["docker", "volume", "ls", "--format", "{{.Name}}"], log=log, capture=True, timeout=30)
            if volume in known.decode().splitlines():
                raise RuntimeError("cannot inspect restore volume")
            continue
        info = json.loads(inspected.stdout)[0]
        if info["Name"] != volume or (info.get("Labels") or {}).get(LABEL) != state["token"]:
            raise ValueError("restore volume marker mismatch")
        command(["docker", "volume", "rm", volume], log=log, timeout=60)
    state["stage"] = "cleaned"
    save(state_path, state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--import-id")
    parser.add_argument("--owner", default="owner-personal")
    parser.add_argument("--source", default="chatgpt-personal")
    parser.add_argument("--query", default="ChatGPT")
    parser.add_argument("--state-root", type=Path, default=DEPLOY / "private/recovery")
    parser.add_argument("--cleanup", type=Path, help="private state.json from this exact helper")
    parser.add_argument("--keep-targets", action="store_true")
    parser.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS,
                        help="maximum streamed tar members, including retained graph files (1..50000000)")
    args = parser.parse_args()
    validate_member_budget(args.max_members)
    if os.geteuid() != 0:
        raise ValueError("run through sudo; deployment credentials remain private")
    os.umask(0o077)
    if args.cleanup:
        state_path = args.cleanup.resolve(strict=True)
        if state_path.stat().st_uid != 0 or state_path.stat().st_mode & 0o077:
            raise ValueError("cleanup state must be root-private")
        with (state_path.parent / "operations.log").open("ab") as log:
            state = json.loads(state_path.read_text())
            cleanup(state, state_path, log)
        print(json.dumps({"result": "cleaned", "checkpoint": str(state_path)}), flush=True)
        return
    if args.backup is None or not args.import_id:
        raise ValueError("backup and completed import ID are required")
    args.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="restore-", dir=args.state_root))
    state_path = work / "state.json"
    token = uuid.uuid4().hex
    database = "aster_archive_restore_" + token[:24]
    state = {"version": 1, "token": token, "database": database,
             "volumes": {kind: database + "_" + kind for kind in ("objects", "auth", "config")},
             "stage": "preflight", "created_at": datetime.now(timezone.utc).isoformat(),
             "owner": args.owner, "source": args.source, "import_id": args.import_id,
             "max_members": args.max_members}
    save(state_path, state)
    print(json.dumps({"stage": "preflight", "checkpoint": str(state_path)}), flush=True)
    with (work / "operations.log").open("ab") as log:
        try:
            # Separate from the writer's normal activity; serializes local recovery procedures.
            with (DEPLOY / "private/recovery.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                backup = checked_backup(args.backup)
                state["backup"] = str(backup)
                with (backup / "SHA256SUMS").open("rb") as manifest_stream:
                    state["backup_manifest_sha256"] = hashlib.file_digest(manifest_stream, "sha256").hexdigest()
                save(state_path, state)
                probe_code = PROBE.read_text()
                with (backup / "database.dump").open("rb") as source:
                    listing = command(COMPOSE + ["exec", "-T", "db", "pg_restore", "--list"],
                                      log=log, source=source, capture=True).decode()
                restore_list, extensions = prepare_restore_list(listing)
                (work / "restore.list").write_text(restore_list)
                state["admin_extensions"] = extensions
                probe_args = ["--owner", args.owner, "--source", args.source,
                              "--import-id", args.import_id, "--query", args.query]
                baseline_raw = command(COMPOSE + ["run", "--rm", "--no-deps", "-T", "mcp", "python", "-c",
                                      probe_code, "capture"] + probe_args, log=log, capture=True, timeout=300)
                baseline = json.loads(baseline_raw)
                if baseline.get("result") != "pass":
                    raise RuntimeError("live reader baseline failed")
                save(work / "baseline.json", baseline)
                docker_root = command(["docker", "info", "--format", "{{.DockerRootDir}}"],
                                      log=log, capture=True, timeout=30).decode().strip()
                free = shutil.disk_usage(docker_root).free
                required = RESERVE + 3 * baseline["database_bytes"] + 4 * (backup / "files.tar.gz").stat().st_size
                if free < required:
                    raise RuntimeError("insufficient conservative restore reserve")
                for volume in state["volumes"].values():
                    existing = command(["docker", "volume", "ls", "--format", "{{.Name}}"], log=log, capture=True, timeout=30)
                    if volume in existing.decode().splitlines():
                        raise ValueError("restore volume already exists")
                    command(["docker", "volume", "create", "--label", LABEL + "=" + token, volume], log=log, timeout=60)
                state["stage"] = "volumes_created"
                save(state_path, state)
                command(COMPOSE + ["exec", "-T", "db", "createdb", "-U", "postgres", "--template=template0",
                                  "--owner=aster_archive_ingest", database], log=log, timeout=60)
                command(COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                                  "-v", "target=" + database, "-v", "marker=" + validate_state(state)], log=log,
                        text=b"COMMENT ON DATABASE :\"target\" IS :'marker';", timeout=60)
                state["stage"] = "restoring_database"
                save(state_path, state)
                print(json.dumps({"stage": state["stage"], "checkpoint": str(state_path)}), flush=True)
                for extension in extensions:
                    command(COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", database,
                                      "-v", "ON_ERROR_STOP=1"], log=log, text=EXTENSION_DDL[extension], timeout=60)
                container_list = "/tmp/aster-archive-restore-" + token + ".list"
                list_created = False
                try:
                    command(COMPOSE + ["exec", "-T", "db", "sh", "-c",
                                      'set -eC; umask 077; cat > "$1"', "sh", container_list],
                            log=log, text=restore_list.encode(), timeout=60)
                    list_created = True
                    with (backup / "database.dump").open("rb") as source:
                        command(COMPOSE + ["exec", "-T", "db", "pg_restore", "-U", "postgres", "--dbname=" + database,
                                          "--exit-on-error", "--no-owner", "--no-acl", "--role=aster_archive_ingest",
                                          "--use-list=" + container_list], log=log, source=source)
                finally:
                    if list_created:
                        command(COMPOSE + ["exec", "-T", "db", "rm", "-f", "--", container_list], log=log, timeout=60)
                command(COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", database, "-v", "ON_ERROR_STOP=1"],
                        log=log, text=b"GRANT USAGE ON SCHEMA aster_archive TO aster_archive_read; GRANT SELECT ON ALL TABLES IN SCHEMA aster_archive TO aster_archive_read;")
                state["stage"] = "restoring_files"
                save(state_path, state)
                print(json.dumps({"stage": state["stage"], "checkpoint": str(state_path)}), flush=True)
                extract = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--user", "0:0",
                           "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "FOWNER", "--cap-add", "DAC_OVERRIDE",
                           "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "512m", "--cpus", "0.5"]
                for kind, target in (("objects", "archive"), ("auth", "auth"), ("config", "config")):
                    extract += ["-v", state["volumes"][kind] + ":/restore/" + target]
                with (backup / "files.tar.gz").open("rb") as source:
                    extraction = json.loads(command(
                        extract + ["--entrypoint", "python", "aster-conversation-archive:local", "-c", probe_code,
                                   "extract", "--max-bytes", str(free - RESERVE),
                                   "--max-members", str(args.max_members)],
                        log=log, source=source, capture=True))
                if extraction.get("result") != "pass" or extraction.get("max_members") != args.max_members:
                    raise RuntimeError("restore extraction receipt mismatch")
                state["extraction"] = extraction
                save(state_path, state)
                verify = COMPOSE + ["run", "--rm", "--no-deps", "-T",
                    "-e", "ASTER_ARCHIVE_ROOT=/var/lib/aster-archive",
                    "-v", state["volumes"]["objects"] + ":/var/lib/aster-archive:ro",
                    "-v", state["volumes"]["auth"] + ":/var/lib/aster-archive-auth:ro",
                    "-v", state["volumes"]["config"] + ":/etc/aster-archive:ro",
                    "mcp", "python", "-c", probe_code, "verify", "--database", database] + probe_args
                state["stage"] = "verifying_restore"
                save(state_path, state)
                with (work / "baseline.json").open("rb") as source:
                    result = json.loads(command(verify, log=log, source=source, capture=True, timeout=1800))
                if result.get("result") != "pass":
                    raise RuntimeError("restored reader verification failed")
                save(work / "restore-proof.json", result)
                state["stage"] = "verified"
                save(state_path, state)
                if not args.keep_targets:
                    cleanup(state, state_path, log)
                print(json.dumps({"result": "pass", "checkpoint": str(state_path), "targets_cleaned": not args.keep_targets,
                                  "coverage": result["coverage"], "original_archive_hash_valid": True,
                                  "restored_auth_valid": True, "restored_acl_valid": True,
                                  "reader_role": result["reader_role"], "cross_owner_denied": True}), flush=True)
        except Exception as exc:
            state["failure_type"] = type(exc).__name__
            save(state_path, state)
            print(json.dumps({"result": "fail", "stage": state["stage"], "error_type": type(exc).__name__,
                              "checkpoint": str(state_path), "test_targets_preserved": True}), flush=True)
            return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"result": "fail", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
