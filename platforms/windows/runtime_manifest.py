"""Validate a frozen Windows candidate and identify its running gateway.

The protected state files contain paths and release identity only, never OAuth
tokens, owner codes, browser cookies, or request bodies.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from urllib.parse import urlsplit


ENV_KEYS = {
    "public_origin": "ASTER_WINDOWS_PUBLIC_ORIGIN",
    "oauth_db": "ASTER_WINDOWS_OAUTH_DB",
    "admin_clients": "ASTER_WINDOWS_ADMIN_CLIENTS",
    "port": "ASTER_WINDOWS_PORT",
    "runtime_manifest": "ASTER_WINDOWS_RUNTIME_MANIFEST",
}

EXCLUDED_DIRECTORIES = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "dist", "build", "artifacts",
}


def _source_inventory(root):
    inventory = set()
    def fail(error):
        raise error
    for directory, folders, files in os.walk(root, followlinks=False, onerror=fail):
        folders[:] = [name for name in folders if name.casefold() not in EXCLUDED_DIRECTORIES]
        for name in folders + files:
            file = Path(directory) / name
            info = file.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("reparse points are not allowed in a frozen source candidate")
        for name in files:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if relative in ("candidate-manifest.json", ".coverage") or name.endswith((".pyc", ".pyo")):
                continue
            inventory.add(relative)
    return inventory


def _absolute(value, name):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return Path(value).resolve()


def load_configuration(config_path, *, source_root, executable=None):
    """Reject ambiguous paths, source drift, or conflicting launch settings."""
    path = _absolute(str(config_path), "config")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("schema_version") != 1:
        raise ValueError("unsupported Windows configuration schema")
    root = _absolute(config.get("source_root"), "source_root")
    if root != Path(source_root).resolve():
        raise ValueError("configuration belongs to a different source directory")
    if path.is_relative_to(root):
        raise ValueError("protected state must be outside the source directory")
    python = _absolute(config.get("python_executable"), "python_executable")
    if python != Path(executable or sys.executable).resolve():
        raise ValueError("configured Python interpreter does not match this process")
    for key in ("oauth_db", "admin_clients", "source_manifest", "runtime_manifest"):
        item = _absolute(config.get(key), key)
        if item.parent != path.parent:
            raise ValueError(f"{key} must be in the protected configuration directory")
        config[key] = str(item)
    protected_paths = [path] + [Path(config[key]) for key in
        ("oauth_db", "admin_clients", "source_manifest", "runtime_manifest")]
    protected_paths.append(Path(config["runtime_manifest"]).with_suffix(".lock"))
    if len(set(protected_paths)) != len(protected_paths):
        raise ValueError("protected configuration, state, and runtime paths must be distinct")
    for index, first in enumerate(protected_paths):
        for second in protected_paths[index + 1:]:
            if first.exists() and second.exists() and first.samefile(second):
                raise ValueError("protected state files must not alias the same file")
    origin = config.get("public_origin", "")
    parsed = urlsplit(origin)
    try:
        public_port = parsed.port
    except ValueError as error:
        raise ValueError("public_origin has an invalid port") from error
    if (not origin.startswith("https://") or parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("public_origin must be a canonical HTTPS origin")
    if public_port is not None and not 1 <= public_port <= 65535:
        raise ValueError("public_origin has an invalid port")
    port = config.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    manifest_bytes = Path(config["source_manifest"]).read_bytes()
    candidate_id = hashlib.sha256(manifest_bytes).hexdigest()
    if config.get("candidate_id") != candidate_id:
        raise ValueError("candidate manifest does not match the configured candidate")
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    if (manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list)
            or not manifest["files"]):
        raise ValueError("invalid candidate manifest")
    seen = set()
    for item in manifest["files"]:
        name = item.get("path", "")
        relative = Path(name)
        if (not name or relative.is_absolute() or any(p in (".", "..", "") for p in name.split("/"))
                or "\\" in name or ":" in name or name in seen):
            raise ValueError("unsafe or duplicate source manifest entry")
        seen.add(name)
        file = (root / relative).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            raise ValueError(f"candidate source file is missing or outside source root: {name}")
        if hashlib.sha256(file.read_bytes()).hexdigest() != item.get("sha256"):
            raise ValueError(f"candidate source changed: {name}")
    if "platforms/windows/oauth_gateway.py" not in seen:
        raise ValueError("candidate manifest does not include the Windows gateway")
    if _source_inventory(root) != seen:
        raise ValueError("source file inventory differs from the frozen candidate")
    allowed = json.loads(Path(config["admin_clients"]).read_text(encoding="utf-8-sig"))
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        raise ValueError("administrator client allowlist must be a JSON string array")
    for key, environment in ENV_KEYS.items():
        expected = str(config[key])
        supplied = os.environ.get(environment)
        if supplied is not None:
            same = supplied == expected
            if key in ("oauth_db", "admin_clients", "runtime_manifest"):
                same = Path(supplied).is_absolute() and Path(supplied).resolve() == Path(expected)
            if not same:
                raise ValueError(f"{environment} conflicts with the protected configuration")
    for key, environment in ENV_KEYS.items():
        os.environ[environment] = str(config[key])
    config["source_root"] = str(root)
    config["python_executable"] = str(python)
    return config


def _process_identity():
    identity = {"process_executable": str(Path(sys.executable).resolve())}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetModuleFileNameW.restype = wintypes.DWORD
        name = ctypes.create_unicode_buffer(32768)
        length = kernel.GetModuleFileNameW(None, name, len(name))
        if not length or length >= len(name):
            raise ctypes.WinError(ctypes.get_last_error())
        identity["process_executable"] = str(Path(name.value).resolve())
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), ctypes.byref(created),
                                      ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        instant = datetime.fromtimestamp(ticks / 10_000_000 - 11_644_473_600, timezone.utc)
        identity["process_started_at"] = instant.isoformat().replace("+00:00", "Z")
        identity["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    return identity


def _write_atomic(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    try:
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if getattr(error, "winerror", None) not in (5, 32) or attempt == 19:
                    raise
                time.sleep(0.1)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def runtime_lease(config):
    """One gateway per protected state directory; remove only our own identity."""
    path = Path(config["runtime_manifest"])
    lock_path = path.with_suffix(".lock")
    lock = lock_path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            if lock.seek(0, 2) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        value = {key: config[key] for key in (
            "source_root", "python_executable", "oauth_db", "admin_clients",
            "public_origin", "port", "candidate_id")}
        value.update(schema_version=1, pid=os.getpid(),
                     started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        value.update(_process_identity())
        _write_atomic(path, value)
        yield value
    finally:
        if locked:
            try:
                current = json.loads(path.read_text(encoding="utf-8-sig"))
                if current.get("pid") == os.getpid() and current.get("candidate_id") == config["candidate_id"]:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
