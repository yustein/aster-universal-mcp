"""Reconcile Windows beta job records after a reboot, without replaying jobs."""
import json
import os
from pathlib import Path
from status_transition import locked, transition
from process_identity import probe_process_identity

def reconcile(root: Path, boot_id: str):
    count = 0
    if not root.exists():
        return count
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        status_file = folder / "status.json"
        try:
            with locked(folder):
                record = json.loads(status_file.read_text(encoding="utf-8"))
                if record.get("status") not in ("queued", "running", "cancelling"):
                    continue
                if record.get("boot_id") == boot_id:
                    if record.get("status") != "cancelling":
                        continue
                    pid = record.get("worker_pid")
                    created = record.get("worker_creation_time_100ns")
                    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                        continue
                    if not isinstance(created, int) or isinstance(created, bool) or created <= 0:
                        continue
                    state, actual = probe_process_identity(pid)
                    if state != "absent":
                        # Present, reused, denied or unknown: never infer
                        # cancellation succeeded or the worker disappeared.
                        continue
                    record["status"] = "cancellation_worker_missing"
                    record["recovery_note"] = "worker_absent_outcome_unverified"
                    temp = folder / ("status-recovery-" + str(os.getpid()) + ".tmp")
                    try:
                        temp.write_text(json.dumps(record), encoding="utf-8")
                        os.replace(temp, status_file)
                    finally:
                        temp.unlink(missing_ok=True)
                    (folder / "request.json").unlink(missing_ok=True)
                    count += 1
                    continue
                record["status"] = ("cancellation_interrupted_by_reboot"
                                    if record["status"] == "cancelling"
                                    else "interrupted_by_reboot")
                temp = folder / ("status-recovery-" + str(os.getpid()) + ".tmp")
                try:
                    temp.write_text(json.dumps(record), encoding="utf-8")
                    os.replace(temp, status_file)
                finally:
                    temp.unlink(missing_ok=True)
                (folder / "request.json").unlink(missing_ok=True)
                count += 1
        except (OSError, ValueError, KeyError):
            continue
    return count
