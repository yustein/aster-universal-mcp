"""Linux administration jobs owned by transient systemd control groups.

Every command (including a synchronous command) uses the same worker path.  A
worker outcome is provisional until the unit has stopped and its cgroup is
empty.  State locks and unique atomic-write files serialize worker/operator
updates.  Only a systemd unit with the recorded invocation identity is stopped;
a stale PID is never a signal target.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

JOB_ROOT = Path(os.environ.get("ASTER_ADMIN_JOB_ROOT", "/var/lib/aster-universal-mcp/admin-jobs"))
MAX_OUTPUT = 16384
MAX_SPOOL = 1048576
PENDING = frozenset(("queued", "running", "finishing", "cancelling", "launch_uncertain", "recovery_required"))
UNIT_PREFIX = "aster-mcp-job-"


def _boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_identity(pid):
    """Return Linux start ticks as well as boot and PID; tolerate exited PIDs."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return {"pid": int(pid), "start_ticks": int(fields[19]), "boot_id": _boot_id(), "state": fields[0]}
    except (OSError, ValueError, IndexError):
        return None


def _same_process(identity):
    if not isinstance(identity, dict):
        return False
    current = process_identity(identity.get("pid", -1))
    return bool(current and current["state"] != "Z" and all(current.get(k) == identity.get(k) for k in ("pid", "start_ticks", "boot_id")))


def _private_dir(path, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("absolute job directory required")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.resolve() != path or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError("protected canonical job directory required")
    return path


def _job_dir(job_id):
    if not isinstance(job_id, str) or len(job_id) != 36 or str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid job id")
    return JOB_ROOT / job_id


def _atomic(path, data):
    """Atomic durable replacement with no shared temporary filename."""
    path = Path(path)
    tmp = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dirfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def _locked(folder):
    _private_dir(JOB_ROOT)
    _private_dir(folder)
    fd = os.open(folder / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_state(folder):
    path = folder / "status.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("job_id") != folder.name or data.get("unit") != UNIT_PREFIX + folder.name + ".service":
        raise ValueError("job identity mismatch")
    return data


def _unit_snapshot(unit):
    names = ("Id", "LoadState", "ActiveState", "SubState", "InvocationID", "ControlGroup", "MainPID", "Result", "Transient", "ExecStart", "Description")
    try:
        result = subprocess.run(["systemctl", "show", unit, "--no-pager", "--property=" + ",".join(names)], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"query_error": True}
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and values.get("LoadState") != "not-found":
        return {"query_error": True}
    if not values.get("LoadState"):
        return {"query_error": True}
    return values


def _unit_identity_ok(data, unit):
    if unit.get("LoadState") == "not-found":
        return True
    if unit.get("Id") != data["unit"] or unit.get("Transient") != "yes":
        return False
    if unit.get("Description") != "Aster MCP job " + data["job_id"]:
        return False
    invocation = unit.get("InvocationID", "")
    if data.get("invocation_id"):
        if not invocation and unit.get("ActiveState") in ("inactive", "failed"):
            return _cgroup_empty(data.get("cgroup")) and not _same_process(data.get("worker_identity"))
        return invocation == data["invocation_id"]
    # Before the worker starts, bind this exact UUID unit and expected worker.
    command = unit.get("ExecStart", "")
    return bool(re.fullmatch(r"[0-9a-f]{32}", invocation) and data["worker_script"] in command and data["job_id"] in command and data["python"] in command)


def _cgroup_empty(path):
    if not path:
        return True
    expected = Path("/sys/fs/cgroup") / str(path).lstrip("/")
    if not str(path).startswith("/") or ".." in Path(path).parts:
        return False
    try:
        events = dict(line.split() for line in (expected / "cgroup.events").read_text().splitlines())
        return events.get("populated") == "0"
    except FileNotFoundError:
        return not expected.exists()
    except (OSError, ValueError):
        return False


def _settle_locked(folder, data, unit):
    """Never turn a label into completion while a known cgroup is populated."""
    if data.get("status") not in PENDING:
        return data
    if data.get("boot_id") != _boot_id():
        data.update(status="interrupted_by_reboot", finished_at=time.time(), unit_quiescent=True, lifecycle_pending=False)
        data.pop("lifecycle_note", None)
    elif unit.get("query_error"):
        data["lifecycle_pending"] = True
        data["lifecycle_note"] = "systemd status unavailable"
    elif not _unit_identity_ok(data, unit):
        data.update(status="recovery_required", lifecycle_pending=True, lifecycle_note="systemd invocation identity mismatch")
    else:
        if unit.get("LoadState") != "not-found":
            data.setdefault("invocation_id", unit.get("InvocationID"))
            if unit.get("ControlGroup"):
                data["cgroup"] = unit["ControlGroup"]
        active = unit.get("ActiveState") not in ("inactive", "failed", "") and unit.get("LoadState") != "not-found"
        quiet = not active and _cgroup_empty(data.get("cgroup")) and not _same_process(data.get("worker_identity"))
        if quiet and data.get("launch_in_progress") and time.time() - data["created_at"] < 15:
            data["lifecycle_pending"] = True
        elif quiet and data.get("launch_uncertain") and not data.get("invocation_id"):
            data.update(status="recovery_required", lifecycle_pending=True, lifecycle_note="uncertain launch has not been observed")
        elif quiet:
            if data.get("cancel_requested"):
                outcome = "cancelled"
            elif data.get("outcome_status"):
                outcome = data["outcome_status"]
            elif data.get("launch_failed"):
                outcome = "launch_failed"
            elif unit.get("Result") == "timeout":
                outcome = "timeout"
            else:
                outcome = "interrupted"
            data.update(status=outcome, finished_at=time.time(), unit_quiescent=True, lifecycle_pending=False)
            data.pop("lifecycle_note", None)
        else:
            data["lifecycle_pending"] = True
    if data["status"] not in PENDING:
        (folder / "request.json").unlink(missing_ok=True)
    _atomic(folder / "status.json", data)
    return data


def _with_output(folder, data):
    result = dict(data)
    # Implementation details stay in local status; public shape keeps useful provenance.
    for key in ("worker_script", "python", "launch_in_progress"):
        result.pop(key, None)
    for stream in ("stdout", "stderr"):
        try:
            fd = os.open(folder / (stream + ".txt"), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(MAX_OUTPUT)
            result[stream] = raw.decode("utf-8", "replace")
        except FileNotFoundError:
            result[stream] = ""
        observed = data.get(stream + "_bytes_observed")
        size = (folder / (stream + ".txt")).stat().st_size if (folder / (stream + ".txt")).exists() else 0
        result[stream + "_truncated"] = (observed is not None and observed > MAX_OUTPUT) or size > MAX_OUTPUT
        result[stream + "_capture_truncated"] = observed > MAX_SPOOL if observed is not None else None
        result[stream + "_spooled_bytes"] = size
    result["response_raw_bytes_per_stream"] = MAX_OUTPUT
    result["spool_bytes_per_stream"] = MAX_SPOOL
    return result


def start_job(command, cwd, max_runtime=3600):
    if not isinstance(max_runtime, int) or isinstance(max_runtime, bool) or not 1 <= max_runtime <= 86400:
        raise ValueError("max_runtime must be 1..86400")
    _private_dir(JOB_ROOT, create=True)
    job_id = str(uuid.uuid4())
    folder = _job_dir(job_id)
    folder.mkdir(mode=0o700)
    unit = UNIT_PREFIX + job_id + ".service"
    worker = str(Path(__file__).with_name("admin_job_worker.py").resolve())
    data = {"job_id": job_id, "unit": unit, "status": "queued", "created_at": time.time(), "boot_id": _boot_id(), "exit_code": None, "python": sys.executable, "worker_script": worker, "max_runtime": max_runtime, "launch_in_progress": True, "lifecycle_pending": True}
    with _locked(folder):
        _atomic(folder / "status.json", data)
        _atomic(folder / "request.json", {"command": command, "cwd": cwd, "max_runtime": max_runtime})
    argv = ["systemd-run", "--quiet", "--collect", "--service-type=exec", "--unit=" + unit,
            "--description=Aster MCP job " + job_id,
            "--property=RuntimeMaxSec=" + str(max_runtime + 10),
            "--property=TimeoutStopSec=2s", "--property=KillMode=control-group",
            "--property=SendSIGKILL=yes", "--property=Restart=no", "--property=UMask=0077",
            "--property=StandardOutput=null", "--property=StandardError=null",
            "--setenv=ASTER_ADMIN_JOB_ROOT=" + str(JOB_ROOT), "--setenv=PYTHONDONTWRITEBYTECODE=1",
            sys.executable, worker, job_id]
    try:
        launch = subprocess.run(argv, capture_output=True, timeout=10, check=False)
        launch_failed = launch.returncode != 0
        uncertain = False
    except (OSError, subprocess.TimeoutExpired):
        launch_failed, uncertain = False, True
    with _locked(folder):
        data = _read_state(folder)
        data["launch_in_progress"] = False
        data["launch_failed"] = launch_failed
        data["launch_uncertain"] = uncertain
        if uncertain and data["status"] == "queued":
            data["status"] = "launch_uncertain"
        _atomic(folder / "status.json", data)
    # An uncertain launch is reconciled, never repeated with a new unit.
    result = job_status(job_id)
    return {k: result[k] for k in ("job_id", "status", "unit", "lifecycle_pending", "unit_quiescent") if k in result}


def job_status(job_id):
    folder = _job_dir(job_id)
    with _locked(folder):
        data = _read_state(folder)
        if data.get("status") in PENDING:
            data = _settle_locked(folder, data, _unit_snapshot(data["unit"]))
        return _with_output(folder, data)


def reconcile_after_boot():
    """Inspect pending work; restarting the gateway never declares live jobs dead."""
    if not JOB_ROOT.exists():
        return 0
    _private_dir(JOB_ROOT)
    count = 0
    for folder in JOB_ROOT.iterdir():
        try:
            if not folder.is_dir() or folder.is_symlink():
                continue
            data = job_status(folder.name)
            count += data.get("status") not in PENDING
        except (OSError, ValueError, KeyError):
            continue
    return count


def cancel_job(job_id):
    folder = _job_dir(job_id)
    with _locked(folder):
        data = _read_state(folder)
        unit = _unit_snapshot(data["unit"])
        data = _settle_locked(folder, data, unit)
        if data["status"] not in PENDING:
            return {"job_id": job_id, "status": data["status"], "cancelled": False, "unit_quiescent": data.get("unit_quiescent", False)}
        if unit.get("query_error") or not _unit_identity_ok(data, unit):
            return {"job_id": job_id, "status": "recovery_required", "cancelled": False, "lifecycle_pending": True}
        if unit.get("LoadState") == "not-found":
            return {"job_id": job_id, "status": data["status"], "cancelled": False, "lifecycle_pending": True}
        # Journal before requesting systemd's stop. The worker cannot overwrite it.
        data.update(status="cancelling", cancel_requested=True, cancel_requested_at=time.time(), lifecycle_pending=True)
        _atomic(folder / "status.json", data)
    try:
        stop = subprocess.run(["systemctl", "stop", data["unit"]], capture_output=True, timeout=8, check=False)
        failed = stop.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    deadline = time.monotonic() + 4
    while True:
        result = job_status(job_id)
        if result["status"] not in PENDING or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if result["status"] in PENDING:
        with _locked(folder):
            state = _read_state(folder)
            if state["status"] in PENDING:
                state.update(status="recovery_required", lifecycle_pending=True, lifecycle_note="stop requires reconciliation" if failed else "cgroup still active")
                _atomic(folder / "status.json", state)
        return {"job_id": job_id, "status": "recovery_required", "cancelled": False, "lifecycle_pending": True}
    return {"job_id": job_id, "status": result["status"], "cancelled": result["status"] == "cancelled", "unit_quiescent": result.get("unit_quiescent", False), "lifecycle_pending": False}


def run_command(command, cwd, timeout):
    started = start_job(command, cwd, max_runtime=timeout)
    job_id = started["job_id"]
    deadline = time.monotonic() + timeout + 5
    while True:
        result = job_status(job_id)
        if result["status"] not in PENDING:
            return result
        if time.monotonic() >= deadline:
            # This is a control-path deadline, not proof of command termination.
            cancel_job(job_id)
            result = job_status(job_id)
            if result["status"] in PENDING:
                result["status"] = "recovery_required"
            return result
        time.sleep(0.05)
