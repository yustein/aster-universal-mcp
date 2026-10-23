"""Windows local beta job record and status helpers."""
import json
import os
import uuid
from pathlib import Path
from status_transition import transition

ROOT = Path(os.environ.get("ASTER_WINDOWS_DATA", str(Path(os.environ.get("PROGRAMDATA", r"C:\\ProgramData")) / "AsterUniversalMCP"))) / "jobs"

def job_path(job_id: str) -> Path:
    if str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid job ID")
    return ROOT / job_id

def save_status(job_id: str, status: str, exit_code=None):
    folder = job_path(job_id)
    transition(folder, status, job_id=job_id, exit_code=exit_code)

def job_status(job_id: str):
    folder = job_path(job_id)
    record = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    for stream in ("stdout", "stderr"):
        path = folder / (stream + ".txt")
        if path.exists():
            with path.open("rb") as f:
                record[stream] = f.read(16384).decode("utf-8", "replace")
            record[stream + "_truncated"] = path.stat().st_size > 16384
    return record
