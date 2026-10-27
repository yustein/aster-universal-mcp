#!/usr/bin/env python3
"""Local Coordinator configuration and offline recovery; never an MCP tool.

No command starts the service, dispatches a callback, or promotes restored state.
Snapshots are private directories, not tar archives. Legacy local writers must
use the same operator.lock shared lock and must be quiesced for a backup.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

STATE_ROOT = Path("/var/lib/aster-coordinator")
UNIT = "aster-coordinator.service"
MEMBERS = {"tasks.sqlite", "oauth.sqlite", "approved-clients.json", "service.env"}
CONFIG_NAMES = {
    "ASTER_MCP_PUBLIC_ORIGIN", "ASTER_MCP_OAUTH_DB", "ASTER_ADMIN_CLIENTS",
    "ASTER_COORDINATION_DB", "ASTER_COORDINATION_PORT", "ASTER_COORDINATION_NATIVE_EVENTS",
}
TASK_TABLES = {"coord_meta", "coord_agents", "coord_grants", "coord_bindings",
               "coord_tasks", "coord_claims", "coord_events"}
NATIVE_TABLES = {"coord_native_subscriptions", "coord_native_outbox", "coord_native_cursor"}
OAUTH_TABLES = {"oauth_clients", "client_metadata", "access_tokens", "refresh_tokens",
                "auth_codes", "approvals", "approval_sessions"}
# PairingStore creates its table only on first use; it is preserved when present.


class OperatorError(Exception):
    """Messages deliberately omit file contents, environment values and SQL rows."""


def safe_path(value: str | Path, *, exists=False, directory=False) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise OperatorError("an absolute path without parent traversal is required")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise OperatorError("symlink paths are not allowed")
    if exists:
        info = path.stat()
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode):
            raise OperatorError("expected a regular file or directory")
    return path


def private_file(path: Path, *, group_read=False):
    safe_path(path, exists=True)
    forbidden = 0o137 if group_read else 0o177
    if path.stat().st_mode & forbidden:
        raise OperatorError("file permissions are not private")


def read_bytes(path: Path, limit=1024 * 1024) -> bytes:
    safe_path(path, exists=True)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise OperatorError("bounded configuration or manifest exceeds its limit")
    return value


def new_file(path: Path, value: bytes):
    safe_path(path)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as out:
        out.write(value)
        out.flush()
        os.fsync(out.fileno())


def sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_origin(value: str):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.netloc == parsed.hostname
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and not parsed.username and not parsed.password
                 and len(parsed.hostname) <= 253 and "." in parsed.hostname
                 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                         for label in parsed.hostname.split(".")))
        if valid:
            try:
                ipaddress.ip_address(parsed.hostname)
                valid = False
            except ValueError:
                pass
    except ValueError:
        valid = False
    if not valid:
        raise OperatorError("a canonical HTTPS DNS origin without port, path or trailing slash is required")


def parse_config(raw: bytes, state_root: Path) -> dict[str, str]:
    safe_path(state_root)
    result = {}
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in CONFIG_NAMES or key in result or not value or re.search(r"[\s\"'`$\\]", value):
            raise OperatorError("configuration must contain unique supported plain assignments")
        result[key] = value
    if set(result) != CONFIG_NAMES:
        raise OperatorError("configuration is missing required fields")
    validate_origin(result["ASTER_MCP_PUBLIC_ORIGIN"])
    expected = {"ASTER_MCP_OAUTH_DB": state_root / "oauth.sqlite",
                "ASTER_COORDINATION_DB": state_root / "tasks.sqlite",
                "ASTER_ADMIN_CLIENTS": state_root / "approved-clients.json"}
    for key, path in expected.items():
        actual = safe_path(result[key])
        if actual != path:
            raise OperatorError("state files must use distinct canonical names in the dedicated state directory")
    port = result["ASTER_COORDINATION_PORT"]
    if not port.isdecimal() or not 1024 <= int(port) <= 65535:
        raise OperatorError("Coordinator port must be an unprivileged TCP port")
    if result["ASTER_COORDINATION_NATIVE_EVENTS"] not in {"0", "1"}:
        raise OperatorError("native events flag must be 0 or 1")
    return result


def load_config(path: Path, state_root=STATE_ROOT):
    private_file(path, group_read=True)
    return parse_config(read_bytes(path), state_root)


def prepare_config(path: Path, origin: str, state_root=STATE_ROOT):
    validate_origin(origin)
    safe_path(path)
    if path.exists():
        current = load_config(path, state_root)
        if current["ASTER_MCP_PUBLIC_ORIGIN"] != origin:
            raise OperatorError("existing origin differs; configuration was preserved")
        return {"created": False, "preserved": True}
    raw = (f"ASTER_MCP_PUBLIC_ORIGIN={origin}\n"
           f"ASTER_MCP_OAUTH_DB={state_root}/oauth.sqlite\n"
           f"ASTER_ADMIN_CLIENTS={state_root}/approved-clients.json\n"
           f"ASTER_COORDINATION_DB={state_root}/tasks.sqlite\n"
           "ASTER_COORDINATION_PORT=18769\nASTER_COORDINATION_NATIVE_EVENTS=1\n").encode()
    parse_config(raw, state_root)
    new_file(path, raw)
    sync_dir(path.parent)
    return {"created": True, "preserved": False}


@contextmanager
def exclusive_lock(state_root: Path):
    safe_path(state_root, exists=True, directory=True)
    if state_root.stat().st_mode & 0o077:
        raise OperatorError("state directory must have private permissions")
    path = state_root / "operator.lock"
    safe_path(path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        if os.geteuid() == 0:
            owner = state_root.stat()
            os.fchown(fd, owner.st_uid, owner.st_gid)
    except FileExistsError:
        private_file(path)
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperatorError("Coordinator or another operator still holds the state lock") from None
        yield
    finally:
        os.close(fd)


def service_stopped(*, allow_missing=False):
    result = subprocess.run(["systemctl", "show", UNIT, "--property=LoadState", "--property=ActiveState", "--property=MainPID"],
                            capture_output=True, text=True, timeout=15, check=False)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    load_states = {"loaded", "not-found"} if allow_missing else {"loaded"}
    if (result.returncode or values.get("LoadState") not in load_states
            or values.get("ActiveState") not in {"inactive", "failed"} or values.get("MainPID") != "0"):
        raise OperatorError("stop the Coordinator unit and quiesce local operator writers before backup")
    return {"unit": UNIT, "active_state": values["ActiveState"], "main_pid": 0}


@contextmanager
def readonly_db(path: Path):
    safe_path(path, exists=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def db_snapshot(source: Path, destination: Path):
    private_file(source)
    new_file(destination, b"")
    with readonly_db(source) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def inspect_application(directory: Path, native: bool):
    summary = {}
    for filename, required in (("tasks.sqlite", TASK_TABLES | (NATIVE_TABLES if native else set())),
                               ("oauth.sqlite", OAUTH_TABLES)):
        with readonly_db(directory / filename) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise OperatorError("SQLite integrity validation failed")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required.issubset(tables):
                raise OperatorError("recovery set lacks the expected application schema")
            if filename == "tasks.sqlite":
                if db.execute("SELECT value FROM coord_meta WHERE key='schema'").fetchone() != ("1",):
                    raise OperatorError("unsupported Coordinator schema version")
                summary["tasks"] = db.execute("SELECT COUNT(*) FROM coord_tasks").fetchone()[0]
                summary["agents"] = db.execute("SELECT COUNT(*) FROM coord_agents").fetchone()[0]
                accepted = 0
                for task_id, sender, receiver, status, raw in db.execute(
                        "SELECT task_id,sender,receiver,status,document FROM coord_tasks"):
                    task = json.loads(raw)
                    if not isinstance(task, dict) or any(task.get(key) != value for key, value in (
                            ("task_id", task_id), ("sender", sender), ("receiver", receiver), ("status", status))):
                        raise OperatorError("task document/index identity mismatch")
                    accepted += task.get("acceptance") == "accepted"
                summary["accepted_tasks"] = accepted
            else:
                summary["oauth_clients"] = db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
    allowed = json.loads(read_bytes(directory / "approved-clients.json"))
    if not isinstance(allowed, list) or any(not isinstance(item, str) or not item or "\x00" in item for item in allowed):
        raise OperatorError("invalid administrator allowlist")
    summary["allowlisted_clients"] = len(allowed)
    return summary


def digest(path: Path):
    safe_path(path, exists=True)
    h = hashlib.sha256()
    size = 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            h.update(chunk)
    return {"size": size, "sha256": h.hexdigest()}


def snapshot(config_path: Path, output: Path, *, state_root=STATE_ROOT, quiescence=service_stopped):
    cfg = load_config(config_path, state_root)
    safe_path(output)
    safe_path(output.parent, exists=True, directory=True)
    if output.exists() or output == state_root or state_root in output.parents:
        raise OperatorError("backup must have a new destination outside the live state directory")
    if output.parent.stat().st_mode & 0o077:
        raise OperatorError("backup parent directory must be private")
    with exclusive_lock(state_root):
        observed = quiescence()
        staging = Path(tempfile.mkdtemp(prefix=".coordinator-backup-", dir=output.parent))
        try:
            db_snapshot(Path(cfg["ASTER_COORDINATION_DB"]), staging / "tasks.sqlite")
            db_snapshot(Path(cfg["ASTER_MCP_OAUTH_DB"]), staging / "oauth.sqlite")
            allowlist = Path(cfg["ASTER_ADMIN_CLIENTS"])
            private_file(allowlist)
            new_file(staging / "approved-clients.json", read_bytes(allowlist))
            new_file(staging / "service.env", read_bytes(config_path))
            summary = inspect_application(staging, cfg["ASTER_COORDINATION_NATIVE_EVENTS"] == "1")
            manifest = {"format": "aster.coordinator.recovery.v1", "created_utc": datetime.now(timezone.utc).isoformat(),
                        "origin": cfg["ASTER_MCP_PUBLIC_ORIGIN"], "state_root": str(state_root),
                        "quiescence": observed, "application": summary,
                        "files": {name: digest(staging / name) for name in sorted(MEMBERS)}}
            new_file(staging / "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
            sync_dir(staging)
            os.rename(staging, output)
            sync_dir(output.parent)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {"backup": str(output), "verified": True, "application": summary}


def verify_snapshot(source: Path):
    safe_path(source, exists=True, directory=True)
    if source.stat().st_mode & 0o077 or {p.name for p in source.iterdir()} != MEMBERS | {"manifest.json"}:
        raise OperatorError("recovery directory permissions or member set is invalid")
    private_file(source / "manifest.json")
    manifest = json.loads(read_bytes(source / "manifest.json"))
    if (not isinstance(manifest, dict) or manifest.get("format") != "aster.coordinator.recovery.v1"
            or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != MEMBERS):
        raise OperatorError("unsupported recovery manifest")
    for name in MEMBERS:
        private_file(source / name)
        if manifest["files"][name] != digest(source / name):
            raise OperatorError("recovery member hash or size mismatch")
    state_root = safe_path(manifest["state_root"])
    cfg = load_config(source / "service.env", state_root)
    if cfg["ASTER_MCP_PUBLIC_ORIGIN"] != manifest["origin"]:
        raise OperatorError("recovery origin does not match its preserved configuration")
    summary = inspect_application(source, cfg["ASTER_COORDINATION_NATIVE_EVENTS"] == "1")
    if summary != manifest["application"]:
        raise OperatorError("recovery application counts disagree with manifest")
    return manifest


def restore_verify(source: Path, work_parent: Path):
    """Actually restore to a fresh temporary directory, inspect offline, then remove it."""
    manifest = verify_snapshot(source)
    safe_path(work_parent, exists=True, directory=True)
    state_root = Path(manifest["state_root"])
    if work_parent == state_root or state_root in work_parent.parents or work_parent == source or source in work_parent.parents:
        raise OperatorError("restore verification must be outside live and backup state")
    if work_parent.stat().st_mode & 0o077:
        raise OperatorError("restore parent directory must be private")
    with tempfile.TemporaryDirectory(prefix="coordinator-restore-", dir=work_parent) as tmp:
        target = Path(tmp)
        for name in sorted(MEMBERS | {"manifest.json"}):
            # Read regular files explicitly; no extraction, links, or service startup.
            with os.fdopen(os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW), "rb") as inp:
                with os.fdopen(os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as out:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fsync(out.fileno())
        restored = verify_snapshot(target)
        if restored != manifest:
            raise OperatorError("restored manifest differs")
    return {"restored_offline": True, "verified": True, "temporary_state_removed": True,
            "service_started": False, "callbacks_dispatched": False, "application": manifest["application"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("install-check")
    for name in ("validate", "prepare-config", "backup"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", type=Path, required=True)
        if name == "prepare-config":
            cmd.add_argument("--origin", required=True)
        if name == "backup":
            cmd.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("restore-verify")
    verify.add_argument("--backup", type=Path, required=True)
    verify.add_argument("--work-parent", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "install-check":
            result = {"safe_to_install": True, **service_stopped(allow_missing=True)}
        elif args.action == "validate":
            load_config(args.config)
            result = {"valid": True, "loopback_only": True}
        elif args.action == "prepare-config":
            result = prepare_config(args.config, args.origin)
        elif args.action == "backup":
            result = snapshot(args.config, args.output)
        else:
            result = restore_verify(args.backup, args.work_parent)
    except OperatorError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
        # Never echo configuration, SQL, task data or exception-supplied file bytes.
        print(json.dumps({"ok": False, "error": "operator check failed; inspect private configuration, stopped service, paths and recovery set"}))
        return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
