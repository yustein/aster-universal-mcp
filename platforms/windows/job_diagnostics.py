"""Read-only operator diagnostics for unresolved Windows jobs.

No process termination, status mutation, or request replay occurs here.
"""
from jobs import job_path, job_status
from process_identity import probe_process_identity
from boot_identity import boot_id

UNRESOLVED = frozenset({
    "cancelling", "cancel_failed", "cancellation_worker_missing",
    "worker_termination_unverified", "cancellation_interrupted_by_reboot",
})

def diagnose_job(job_id):
    record = job_status(job_id)
    status = record.get("status", "unknown")
    result = {"job_id": job_id, "status": status,
              "needs_operator_review": status in UNRESOLVED,
              "action": "none"}
    if status not in UNRESOLVED:
        return result
    pid = record.get("worker_pid")
    created = record.get("worker_creation_time_100ns")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        result["action"] = "review_missing_worker_identity"
        return result
    if not isinstance(created, int) or isinstance(created, bool) or created <= 0:
        result["action"] = "review_missing_worker_identity"
        return result
    try:
        if record.get("boot_id") != boot_id():
            result["action"] = "review_previous_boot_outcome"
            return result
        state, actual = probe_process_identity(pid)
    except Exception:
        state, actual = "error", None
    result["process_probe"] = state
    if state == "present" and actual == created:
        result["action"] = "verified_worker_present_manual_review"
    elif state == "present":
        result["action"] = "pid_reused_do_not_terminate"
    elif state == "absent":
        result["action"] = "worker_absent_verify_descendants_and_outcome"
    else:
        result["action"] = "inspection_unavailable_do_not_terminate"
    return result
