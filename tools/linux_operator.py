#!/usr/bin/python3
"""Protected, standard-library-only operator for the manual Linux deployment.

The installed copy lives outside application source and the application venv.
Recovery never imports application modules. Root remains fully trusted.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

SCHEMA = 1
DEFAULT_STATE = "/var/lib/aster-universal-mcp"
SOURCE_EXCLUDES = {".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def protected_path(path, *, directory=False, ancestors=True):
    """Reject links and non-root/writable control paths, including ancestors."""
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise PermissionError(f"non-canonical protected path: {path}")
    check = [path, *path.parents] if ancestors else [path]
    for item in check:
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
            raise PermissionError(f"root ownership required: {item}")
        if info.st_mode & 0o022:
            raise PermissionError(f"group/world writable protected path: {item}")
    if directory and not path.is_dir():
        raise PermissionError(f"directory required: {path}")
    if not directory and not path.is_file():
        raise PermissionError(f"regular file required: {path}")
    return path


def private_file(path):
    path = protected_path(path)
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise PermissionError(f"single-link mode 0600 file required: {path}")
    return path


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise PermissionError("refusing linked control file")
    fd, name = tempfile.mkstemp(prefix=".aster-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    return json.loads(private_file(path).read_text(encoding="utf-8"))


def source_inventory(root):
    root = protected_path(root, directory=True)
    result = {}
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if name not in SOURCE_EXCLUDES)
        for name in folders:
            protected_path(Path(directory) / name, directory=True)
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == "candidate-manifest.json":
                continue
            protected_path(path)
            result[relative] = {"sha256": sha256(path), "mode": stat.S_IMODE(path.stat().st_mode)}
    return result


def verify_source(config):
    root = Path(config["source"])
    manifest_path = protected_path(root / "candidate-manifest.json")
    if sha256(manifest_path) != config["manifest_sha256"]:
        raise PermissionError("candidate manifest changed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("invalid source manifest")
    expected = {}
    for item in files:
        name = item["path"]
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or name in expected:
            raise ValueError("invalid manifest path")
        expected[name] = item
    actual = source_inventory(root)
    if set(actual) != set(expected):
        raise PermissionError("source inventory changed")
    for name, entry in actual.items():
        if entry["sha256"] != expected[name]["sha256"]:
            raise PermissionError(f"source hash changed: {name}")
        recorded_mode = expected[name].get("mode")
        if recorded_mode is not None:
            mode = int(recorded_mode, 8) if isinstance(recorded_mode, str) else recorded_mode
            if entry["mode"] != mode:
                raise PermissionError(f"source mode changed: {name}")
    return {"manifest_sha256": config["manifest_sha256"], "files": len(actual),
            "base_commit": manifest.get("base_commit"), "source_commit": manifest.get("source_commit")}


def venv_inventory(root):
    """Pin regular bytes and symlink targets without following directory links."""
    root = protected_path(root, directory=True)
    entries = {}
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in sorted(folders + names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                info = path.lstat()
                if info.st_uid or info.st_gid:
                    raise PermissionError("venv symlink must be root owned")
                target = path.resolve(strict=True)
                protected_path(target, directory=target.is_dir())
                entries[relative] = {"link": os.readlink(path), "resolved": str(target)}
            elif path.is_dir():
                protected_path(path, directory=True)
            else:
                protected_path(path)
                entries[relative] = {"sha256": sha256(path), "mode": stat.S_IMODE(path.stat().st_mode)}
    executable = (root / "bin/python").resolve(strict=True)
    entries["@resolved-python"] = {"path": str(executable), "sha256": sha256(executable)}
    return entries


def verify_venv(config, installation):
    record = private_file(Path(config["state"]) / "venv-manifest.json")
    if sha256(record) != installation["venv_manifest_sha256"]:
        raise PermissionError("venv inventory record changed")
    if json.loads(record.read_text()) != venv_inventory(Path(config["venv"])):
        raise PermissionError("venv bytes, modes or link targets changed")


def load_control(state, *, verify_self=True):
    state = protected_path(state, directory=True)
    if stat.S_IMODE(state.stat().st_mode) != 0o700:
        raise PermissionError("state directory must be mode 0700")
    installation = read_json(state / "installation.json")
    config_file = private_file(state / "config.json")
    if sha256(config_file) != installation["config_sha256"]:
        raise PermissionError("protected configuration changed")
    config = json.loads(config_file.read_text())
    if config.get("schema_version") != SCHEMA or config.get("state") != str(state):
        raise ValueError("invalid deployment configuration")
    if verify_self:
        operator = private_file(state / "operator-linux.py")
        if Path(__file__).resolve() != operator or sha256(operator) != installation["operator_sha256"]:
            raise PermissionError("run the unchanged protected operator copy with /usr/bin/python3")
    # These are the only mutable control targets recovery may touch.
    expected = {"oauth_db": state / "oauth.sqlite", "allowlist": state / "admin-client-allowlist.json",
                "workspace": state / "workspace", "socket": state / "run/admin.sock",
                "jobs": state / "jobs", "transfers": state / "transfers",
                "audit": state / "logs/executor.jsonl"}
    if config.get("paths") != {key: str(value) for key, value in expected.items()}:
        raise PermissionError("non-canonical state paths")
    for name in ("workspace", "run", "jobs", "transfers", "logs"):
        protected_path(state / name, directory=True)
    return config, installation


@contextlib.contextmanager
def operator_lock(state):
    path = Path(state) / "operator.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_file(path)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def journal(state, action, phase, **details):
    """Fsync intent before mutations; never write pairing codes or token values."""
    path = Path(state) / "operator-journal.jsonl"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        private_file(path)
        payload = {"utc": utc(), "action": action, "phase": phase, **details}
        os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def process_identity(pid):
    proc = Path("/proc") / str(int(pid))
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    details = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
    argv = (proc / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
    env = {}
    for item in (proc / "environ").read_bytes().split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator and key.startswith(b"ASTER_"):
            env[key.decode()] = value.decode()
    executable = (proc / "exe").stat()
    return {"pid": int(pid), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_ticks": fields[19], "state": fields[0],
            "exe": os.readlink(proc / "exe"), "exe_device": executable.st_dev, "exe_inode": executable.st_ino,
            "argv": argv, "cwd": os.readlink(proc / "cwd"),
            "uids": [int(x) for x in details["Uid"].split()],
            "gids": [int(x) for x in details["Gid"].split()], "environment": env}


def same_process(expected, actual):
    keys = ("pid", "boot_id", "start_ticks", "exe_device", "exe_inode", "argv", "cwd", "uids", "gids", "environment")
    return all(expected.get(key) == actual.get(key) for key in keys)


def process_alive(expected):
    try:
        actual = process_identity(expected["pid"])
        return same_process(expected, actual) and actual["state"] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def fd_sockets(pid):
    result = set()
    for path in (Path("/proc") / str(pid) / "fd").iterdir():
        try:
            match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(path))
            if match:
                result.add(match.group(1))
        except OSError:
            pass
    return result


def listener_matches(identity, port):
    owned = fd_sockets(identity["pid"])
    found = []
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        columns = line.split()
        address, encoded_port = columns[1].split(":")
        if int(encoded_port, 16) == port and columns[3] == "0A":
            found.append(address == "0100007F" and columns[9] in owned)
    # Reject any IPv6 listener on the same port, including a wildcard listener.
    for line in Path("/proc/net/tcp6").read_text().splitlines()[1:]:
        columns = line.split()
        if int(columns[1].split(":")[1], 16) == port and columns[3] == "0A":
            found.append(False)
    return found == [True]


def socket_matches(identity, path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid or info.st_gid or stat.S_IMODE(info.st_mode) != 0o600:
        return False
    owned = fd_sockets(identity["pid"])
    entries = [line.split() for line in Path("/proc/net/unix").read_text().splitlines()[1:]]
    return any(len(entry) == 8 and entry[7] == str(path) and entry[6] in owned for entry in entries)


def runtime_file(config):
    return Path(config["state"]) / "runtime.json"


def read_runtime(config):
    path = runtime_file(config)
    return read_json(path) if path.exists() else {"processes": {}}


def runtime_status(config):
    runtime = read_runtime(config)
    observations = {}
    for role in ("executor", "gateway"):
        recorded = runtime.get("processes", {}).get(role)
        item = {"recorded": recorded is not None, "identity_matches": False, "endpoint_matches": False}
        if recorded:
            item["identity"] = recorded
            expected_script = "admin_executor_local.py" if role == "executor" else "oauth_service.py"
            canonical = (runtime.get("manifest_sha256") == config["manifest_sha256"]
                         and runtime.get("config_sha256") == sha256(Path(config["state"]) / "config.json")
                         and recorded.get("argv") == [config["python"], "-B", str(Path(config["source"]) / expected_script)]
                         and recorded.get("cwd") == config["paths"]["workspace"]
                         and recorded.get("uids") == [0] * 4 and recorded.get("gids") == [0] * 4
                         and recorded.get("environment") == {k: v for k, v in environment(config, runtime.get("launch_id")).items()
                                                             if k.startswith("ASTER_")})
            item["identity_matches"] = canonical and process_alive(recorded)
            if item["identity_matches"]:
                try:
                    item["endpoint_matches"] = (socket_matches(recorded, config["paths"]["socket"])
                                                if role == "executor" else listener_matches(recorded, config["port"]))
                except OSError:
                    pass
        observations[role] = item
    return {"processes": observations, "ready": all(item["identity_matches"] and item["endpoint_matches"]
                                                     for item in observations.values())}


def environment(config, launch_id):
    paths = config["paths"]
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PYTHONPATH": config["source"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "ASTER_OPERATOR_LAUNCH_ID": launch_id, "ASTER_MCP_PUBLIC_ORIGIN": config["origin"],
            "ASTER_MCP_PORT": str(config["port"]), "ASTER_MCP_OAUTH_DB": paths["oauth_db"],
            "ASTER_ALLOWED_ROOT": paths["workspace"], "ASTER_WORKSPACE": paths["workspace"],
            "ASTER_ADMIN_CLIENTS": paths["allowlist"], "ASTER_ADMIN_SOCKET": paths["socket"],
            "ASTER_ADMIN_JOB_ROOT": paths["jobs"], "ASTER_TRANSFER_ROOT": paths["transfers"],
            "ASTER_ADMIN_AUDIT": paths["audit"]}


def verify_deployment(config, installation):
    source = verify_source(config)
    verify_venv(config, installation)
    private_file(config["paths"]["allowlist"])
    if Path(config["paths"]["oauth_db"]).exists():
        private_file(config["paths"]["oauth_db"])
    return source


def require_enabled(config):
    if (Path(config["state"]) / "access-disabled.json").exists():
        raise PermissionError("remote access is disabled; explicitly enable before new starts or grants")


def require_ready(config):
    if not runtime_status(config)["ready"]:
        raise PermissionError("both canonical processes and their endpoints must be verified first")


def reconcile_start(config):
    """Recover a Popen-to-record interruption by its unique pre-journaled launch ID."""
    runtime = read_runtime(config)
    launch_id = runtime.get("launch_id")
    if not launch_id:
        return runtime
    expected_env = {k: v for k, v in environment(config, launch_id).items() if k.startswith("ASTER_")}
    changed = False
    for role, script in (("executor", "admin_executor_local.py"), ("gateway", "oauth_service.py")):
        known = runtime.get("processes", {}).get(role)
        if known and process_alive(known):
            continue
        matches = []
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                item = process_identity(int(path.name))
                if (item["environment"] == expected_env and item["uids"] == [0] * 4
                        and item["gids"] == [0] * 4 and item["cwd"] == config["paths"]["workspace"]
                        and item["argv"] == [config["python"], "-B", str(Path(config["source"]) / script)]):
                    matches.append(item)
            except (OSError, ValueError, UnicodeError, IndexError):
                pass
        if len(matches) > 1:
            raise RuntimeError(f"ambiguous {role} launch; inspect locally before mutation")
        if matches:
            runtime.setdefault("processes", {})[role] = matches[0]
            changed = True
    if changed:
        atomic_json(runtime_file(config), runtime)
        journal(config["state"], "reconcile", "identified", launch_id=launch_id)
    operation = runtime.get("operation", {})
    if (operation.get("action") == "start" and operation.get("status") in ("in_progress", "needs_reconciliation")
            and runtime_status(config)["ready"]):
        runtime["operation"] = {"action": "start", "status": "completed", "reconciled": True, "completed_at": utc()}
        atomic_json(runtime_file(config), runtime)
        journal(config["state"], "reconcile", "completed", launch_id=launch_id)
    return runtime


def start(config, installation):
    require_enabled(config)
    verify_deployment(config, installation)
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Linux pidfd support is required")
    subprocess.run(["/usr/bin/systemctl", "show", "--property=Version", "--value"], check=True,
                   capture_output=True, text=True, timeout=5)
    runtime = reconcile_start(config)
    status = runtime_status(config)
    if status["ready"]:
        return {"status": "already_running", **status}
    if any(item["identity_matches"] for item in status["processes"].values()):
        raise RuntimeError("partial previous start; use status then stop before another start")
    # Never steal a socket or port from an unknown process.
    socket_path = Path(config["paths"]["socket"])
    if socket_path.exists():
        known = runtime.get("processes", {}).get("executor")
        entries = Path("/proc/net/unix").read_text()
        if not known or any(line.split()[-1:] == [str(socket_path)] for line in entries.splitlines()[1:]):
            raise RuntimeError("unexpected existing executor socket; inspect before new start")
        journal(config["state"], "start", "remove_stale_socket_intent")
        socket_path.unlink()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A" and int(fields[1].split(":")[1], 16) == config["port"]:
                raise RuntimeError("configured port is already occupied")
    launch_id = str(uuid.uuid4())
    runtime = {"schema_version": SCHEMA, "launch_id": launch_id, "created_at": utc(),
               "manifest_sha256": config["manifest_sha256"], "config_sha256": installation["config_sha256"],
               "processes": {}, "operation": {"action": "start", "status": "in_progress", "phase": "intent",
                    "deadline_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=45)).isoformat()}}
    atomic_json(runtime_file(config), runtime)
    journal(config["state"], "start", "intent", launch_id=launch_id)
    try:
        for role, script in (("executor", "admin_executor_local.py"), ("gateway", "oauth_service.py")):
            runtime["operation"]["phase"] = role + "_launch_intent"
            atomic_json(runtime_file(config), runtime)
            journal(config["state"], "start", role + "_launch_intent", launch_id=launch_id)
            log = Path(config["state"]) / "logs" / (role + ".log")
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                process = subprocess.Popen([config["python"], "-B", str(Path(config["source"]) / script)],
                                           cwd=config["paths"]["workspace"], env=environment(config, launch_id),
                                           stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                                           close_fds=True, start_new_session=True, umask=0o077)
            finally:
                os.close(fd)
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"{role} exited with {process.returncode}; inspect its protected log")
                try:
                    identity = process_identity(process.pid)
                    if identity["argv"] == [config["python"], "-B", str(Path(config["source"]) / script)]:
                        runtime["processes"][role] = identity
                        atomic_json(runtime_file(config), runtime)
                        matches = (socket_matches(identity, config["paths"]["socket"]) if role == "executor"
                                   else listener_matches(identity, config["port"]))
                        if matches:
                            break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{role} did not become ready; reconcile status before retry")
                time.sleep(0.05)
        require_ready(config)
        runtime["operation"] = {"action": "start", "status": "completed", "completed_at": utc()}
        atomic_json(runtime_file(config), runtime)
        journal(config["state"], "start", "completed", launch_id=launch_id)
        return {"status": "started", **runtime_status(config)}
    except Exception as error:
        runtime["operation"] = {"action": "start", "status": "needs_reconciliation", "error": str(error)}
        atomic_json(runtime_file(config), runtime)
        journal(config["state"], "start", "needs_reconciliation", launch_id=launch_id, error=str(error))
        raise


def stop_one(expected, *, grace=8):
    """Signal a retained pidfd only after creation/executable/config identity match."""
    try:
        descriptor = os.pidfd_open(expected["pid"], 0)
    except ProcessLookupError:
        return "already_exited"
    try:
        try:
            actual = process_identity(expected["pid"])
        except FileNotFoundError:
            return "already_exited"
        if not same_process(expected, actual):
            raise PermissionError("recorded PID has a different identity; refusing to signal it")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        if not select.select([descriptor], [], [], grace)[0]:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            if not select.select([descriptor], [], [], 3)[0]:
                raise TimeoutError("identified process has not exited")
        return "stopped"
    finally:
        os.close(descriptor)


def stop(config):
    runtime = reconcile_start(config)
    results = {}
    failures = []
    for role in ("gateway", "executor"):
        identity = runtime.get("processes", {}).get(role)
        if identity is None:
            results[role] = "no_recorded_process"
            continue
        journal(config["state"], "stop", role + "_intent", identity=identity)
        try:
            results[role] = stop_one(identity)
            journal(config["state"], "stop", role + "_settled", result=results[role])
        except Exception as error:
            results[role] = str(error)
            failures.append(role)
            journal(config["state"], "stop", role + "_unresolved", error=str(error))
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A" and int(fields[1].split(":")[1], 16) == config["port"]:
                failures.append("unidentified_listener")
    if any(line.split()[-1:] == [config["paths"]["socket"]]
           for line in Path("/proc/net/unix").read_text().splitlines()[1:]):
        failures.append("unidentified_executor_socket")
    runtime["operation"] = {"action": "stop", "status": "needs_reconciliation" if failures else "completed",
                            "completed_at": utc(), "results": results}
    atomic_json(runtime_file(config), runtime)
    if failures:
        raise RuntimeError("stop unresolved for " + ", ".join(failures) + "; inspect local identity and disable public route")
    return {"status": "stopped", "processes": results,
            "jobs": "separate systemd jobs are not automatically cancelled; inspect and cancel them explicitly"}


def allowlist(config):
    values = read_json(config["paths"]["allowlist"])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("invalid allowlist; use disable for emergency denial")
    return values


def clients(config):
    db_path = private_file(config["paths"]["oauth_db"])
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        rows = db.execute("SELECT client_id, name, redirect_uris, revoked FROM oauth_clients").fetchall()
    allowed = allowlist(config)
    return [{"client_id": row[0], "name": row[1], "redirect_uris": json.loads(row[2]),
             "revoked": bool(row[3]), "admin_allowed": row[0] in allowed} for row in rows]


def change_grant(config, installation, client_id, *, approve):
    if not client_id or len(client_id) > 256:
        raise ValueError("invalid client ID")
    if approve:
        require_enabled(config)
        verify_deployment(config, installation)
        require_ready(config)
        db_path = private_file(config["paths"]["oauth_db"])
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            row = db.execute("SELECT revoked FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
            meta = db.execute("SELECT 1 FROM client_metadata WHERE client_id=?", (client_id,)).fetchone()
        if row is None or row[0] or not meta:
            raise PermissionError("client is not registered and active")
    values = allowlist(config)
    updated = sorted(set(values) | {client_id}) if approve else sorted(set(values) - {client_id})
    action = "approve" if approve else "revoke"
    journal(config["state"], action, "intent", client_id=client_id)
    atomic_json(config["paths"]["allowlist"], updated)
    journal(config["state"], action, "completed", client_id=client_id)
    return {"action": action, "client_id": client_id, "admin_allowed": approve,
            "oauth_credentials_revoked": False}


def disable(config):
    journal(config["state"], "disable", "intent")
    atomic_json(Path(config["state"]) / "access-disabled.json", {"disabled_at": utc()})
    atomic_json(config["paths"]["allowlist"], [])
    stopped = stop(config)
    journal(config["state"], "disable", "completed")
    return {"status": "disabled", "stop": stopped, "oauth_credentials_revoked": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "start", "stop", "clients", "disable", "enable"):
        sub.add_parser(action)
    for action in ("approve", "revoke"):
        sub.add_parser(action).add_argument("client_id")
    pair = sub.add_parser("pair")
    pair.add_argument("--ttl", type=int, default=900)
    args = parser.parse_args()
    if os.geteuid() != 0 or os.getegid() != 0:
        parser.error("run locally as root with /usr/bin/python3")
    os.umask(0o077)
    try:
        config, installation = load_control(Path(args.state))
        with operator_lock(config["state"]):
            if args.action == "status":
                reconcile_start(config)
                result = {"checked_at": utc(), "config_sha256": installation["config_sha256"],
                          "disabled": (Path(config["state"]) / "access-disabled.json").exists(),
                          "deployment": config, **runtime_status(config)}
                for name, check in (("source", lambda: verify_source(config)),
                                    ("venv", lambda: verify_venv(config, installation))):
                    try:
                        result[name + "_verified"] = True
                        check()
                    except Exception as error:
                        result[name + "_verified"] = False
                        result[name + "_error"] = str(error)
            elif args.action == "start":
                result = start(config, installation)
            elif args.action == "stop":
                result = stop(config)
            elif args.action == "clients":
                result = clients(config)
            elif args.action in ("approve", "revoke"):
                result = change_grant(config, installation, args.client_id, approve=args.action == "approve")
            elif args.action == "disable":
                result = disable(config)
            elif args.action == "enable":
                verify_deployment(config, installation)
                journal(config["state"], "enable", "intent")
                (Path(config["state"]) / "access-disabled.json").unlink(missing_ok=True)
                journal(config["state"], "enable", "completed")
                result = {"status": "enabled", "grants_restored": False, "processes_started": False}
            else:
                require_enabled(config)
                verify_deployment(config, installation)
                require_ready(config)
                if not 30 <= args.ttl <= 900:
                    raise ValueError("pairing TTL must be 30..900 seconds")
                journal(config["state"], "pair", "intent")
                subprocess.run([config["python"], "-B", str(Path(config["source"]) / "pairing_admin.py"),
                                "--db", config["paths"]["oauth_db"], "issue", "--ttl", str(args.ttl)],
                               cwd=config["paths"]["workspace"], env=environment(config, "local-pair"),
                               check=True, timeout=10)
                journal(config["state"], "pair", "completed")
                return
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as error:
        print(json.dumps({"status": "error", "action": args.action, "error": str(error)}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
