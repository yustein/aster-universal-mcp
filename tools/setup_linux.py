#!/usr/bin/python3
"""Install a fresh, manually operated Linux deployment from verified source.

Does not create public routes, install gateway services or modify existing state.
Invoke with /usr/bin/python3; shell executable bits are not required.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

# The installer itself must not put a compiled module inside the frozen source.
sys.dont_write_bytecode = True
from linux_operator import (SCHEMA, atomic_json, journal, protected_path, sha256,
                            utc, venv_inventory, verify_source)


def canonical_new(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise ValueError("absolute, canonical paths without symlink ancestors are required")
    protected_path(path.parent, directory=True)
    return path


def dependency_inventory(python, lock, environment, log):
    check = subprocess.run([str(python), "-B", "-m", "pip", "--isolated", "check"],
                           capture_output=True, text=True, env=environment, timeout=30)
    log.write(check.stdout + check.stderr)
    if check.returncode:
        raise RuntimeError("runtime dependency check failed; inspect installation.log")
    result = subprocess.run([str(python), "-B", "-m", "pip", "--isolated", "list", "--format=json"],
                            capture_output=True, text=True, check=True, env=environment, timeout=30)
    installed = {re.sub(r"[-_.]+", "-", item["name"]).lower(): item["version"]
                 for item in json.loads(result.stdout)}
    expected = {}
    for line in lock.read_text().splitlines():
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", value)
        if not match:
            raise ValueError("Linux runtime lock must contain exact name==version pins only")
        expected[re.sub(r"[-_.]+", "-", match[1]).lower()] = match[2]
    if not expected or any(installed.get(name) != version for name, version in expected.items()):
        raise RuntimeError("installed runtime does not match Linux dependency lock")
    extras = set(installed) - set(expected) - {"pip", "setuptools"}
    if extras:
        raise RuntimeError("unexpected packages in runtime venv: " + ", ".join(sorted(extras)))
    return {"recorded_at": utc(), "lock_sha256": sha256(lock), "packages": installed,
            "pip_check": check.stdout.strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/aster-universal-mcp"))
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", type=int, default=18768)
    parser.add_argument("--existing-venv", action="store_true",
                        help="Inspect and pin an existing runtime-only venv without installing into it")
    parser.add_argument("--wheel-dir", type=Path,
                        help="Install exclusively from this existing local wheel directory")
    args = parser.parse_args()
    if os.geteuid() != 0 or os.getegid() != 0:
        parser.error("run locally as root using /usr/bin/python3")
    os.umask(0o077)
    try:
        source = protected_path(args.source, directory=True)
        venv = canonical_new(args.venv)
        state = canonical_new(args.state)
        locations = [source, venv, state]
        if any(a == b or a in b.parents or b in a.parents
               for i, a in enumerate(locations) for b in locations[i + 1:]):
            raise ValueError("source, venv and state must be separate, non-nested directories")
        origin = urlsplit(args.origin)
        if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.path or origin.query or origin.fragment or args.origin.endswith("/")
                or not 1024 <= args.port <= 65535):
            raise ValueError("provide an HTTPS origin without a path/trailing slash and port 1024..65535")
        if state.exists():
            raise FileExistsError("state already exists; refusing to overwrite or migrate it")
        if venv.exists() != args.existing_venv:
            raise ValueError("use an absent venv path or explicitly inspect an existing venv")
        manifest = protected_path(source / "candidate-manifest.json")
        lock = protected_path(source / "requirements-linux-lock.txt")
        config = {"schema_version": SCHEMA, "installed_at": utc(), "state": str(state),
                  "source": str(source), "venv": str(venv), "python": str(venv / "bin/python"),
                  "origin": args.origin, "port": args.port, "manifest_sha256": sha256(manifest),
                  "paths": {"oauth_db": str(state / "oauth.sqlite"),
                            "allowlist": str(state / "admin-client-allowlist.json"),
                            "workspace": str(state / "workspace"), "socket": str(state / "run/admin.sock"),
                            "jobs": str(state / "jobs"), "transfers": str(state / "transfers"),
                            "audit": str(state / "logs/executor.jsonl")}}
        verify_source(config)
        if args.wheel_dir:
            protected_path(args.wheel_dir, directory=True)
        if not hasattr(os, "pidfd_open"):
            raise RuntimeError("Linux pidfd support is required")
        manager = subprocess.run(["/usr/bin/systemctl", "show", "--property=Version", "--value"],
                                 capture_output=True, text=True, check=True, timeout=5)
        if not manager.stdout.strip():
            raise RuntimeError("a functioning systemd system manager is required")
        state.mkdir(mode=0o700)
        for name in ("workspace", "run", "jobs", "transfers", "logs"):
            (state / name).mkdir(mode=0o700)
        journal(state, "install", "intent", source=str(source), venv=str(venv),
                manifest_sha256=config["manifest_sha256"])
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        with open(state / "logs/installation.log", "w", encoding="utf-8") as log:
            if not args.existing_venv:
                journal(state, "install", "venv_intent")
                subprocess.run(["/usr/bin/python3", "-m", "venv", str(venv)],
                               stdout=log, stderr=log, env=environment, check=True, timeout=90)
                command = [str(venv / "bin/python"), "-B", "-m", "pip", "--isolated", "install",
                           "--disable-pip-version-check", "--no-input", "--no-cache-dir", "--no-compile",
                           "-r", str(lock)]
                if args.wheel_dir:
                    command.extend(["--no-index", "--find-links", str(args.wheel_dir)])
                journal(state, "install", "dependencies_intent")
                subprocess.run(command, stdout=log, stderr=log, env=environment, check=True, timeout=300)
            inventory = dependency_inventory(venv / "bin/python", lock, environment, log)
        atomic_json(state / "dependency-record.json", inventory)
        atomic_json(state / "venv-manifest.json", venv_inventory(venv))
        atomic_json(state / "config.json", config)
        atomic_json(state / "admin-client-allowlist.json", [])
        shutil.copyfile(source / "tools/linux_operator.py", state / "operator-linux.py")
        os.chmod(state / "operator-linux.py", 0o600)
        installation = {"schema_version": SCHEMA, "installed_at": utc(),
                        "config_sha256": sha256(state / "config.json"),
                        "operator_sha256": sha256(state / "operator-linux.py"),
                        "venv_manifest_sha256": sha256(state / "venv-manifest.json"),
                        "dependency_record_sha256": sha256(state / "dependency-record.json"),
                        "systemd": manager.stdout.strip(), "mode": "manual root gateway and executor"}
        atomic_json(state / "installation.json", installation)
        journal(state, "install", "completed")
        print(json.dumps({"status": "installed_not_started", "state": str(state),
                          "operator": str(state / "operator-linux.py"), "manifest_sha256": config["manifest_sha256"],
                          "dependencies": inventory, "systemd": manager.stdout.strip()}, indent=2))
    except Exception as error:
        print(json.dumps({"status": "install_failed", "error": str(error),
                          "recovery": "Inspect retained paths/logs; no existing installation was removed."}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
