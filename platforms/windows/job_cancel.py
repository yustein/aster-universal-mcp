"""Fail-closed Windows job cancellation with serialized cancellation claims."""
from jobs import job_path, job_status
from boot_identity import boot_id
from process_identity import terminate_verified_worker
from status_transition import transition


def cancel_job(job_id: str):
    folder = job_path(job_id)
    record = job_status(job_id)
    if record["status"] not in ("queued", "running"):
        return {"job_id": job_id, "status": record["status"], "cancelled": False}
    pid = record.get("worker_pid")
    created = record.get("worker_creation_time_100ns")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"job_id": job_id, "status": "pending", "cancelled": False}
    if not isinstance(created, int) or isinstance(created, bool) or created <= 0:
        return {"job_id": job_id, "status": "identity_unverified", "cancelled": False}
    try:
        current_boot = boot_id()
    except Exception:
        return {"job_id": job_id, "status": "identity_unverified", "cancelled": False}
    if not record.get("boot_id") or record["boot_id"] != current_boot:
        return {"job_id": job_id, "status": "identity_mismatch", "cancelled": False}

    claimed, changed = transition(folder, "cancelling")
    if not changed:
        return {"job_id": job_id, "status": claimed["status"], "cancelled": False}
    # Identity must be checked against the claimed record, not an earlier read.
    if (claimed.get("worker_pid") != pid
            or claimed.get("worker_creation_time_100ns") != created
            or claimed.get("boot_id") != current_boot):
        transition(folder, "cancel_failed")
        return {"job_id": job_id, "status": "cancel_failed", "cancelled": False}
    if not terminate_verified_worker(pid, created):
        transition(folder, "cancel_failed")
        return {"job_id": job_id, "status": "cancel_failed", "cancelled": False}
    final, _ = transition(folder, "cancelled", exit_code=None)
    (folder / "request.json").unlink(missing_ok=True)
    return {"job_id": job_id, "status": final["status"], "cancelled": final["status"] == "cancelled"}
