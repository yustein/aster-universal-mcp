"""Focused Linux executor regressions; actual systemd cases are opt-in below."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

if sys.platform != "linux":
    pytest.skip("Linux execution implementation", allow_module_level=True)

import admin_jobs as jobs
import admin_job_worker as worker
import admin_executor_local as executor


def fixture_state(tmp_path, monkeypatch, status="running"):
    root = tmp_path / "jobs"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(jobs, "JOB_ROOT", root)
    job_id = str(uuid.uuid4())
    folder = root / job_id
    folder.mkdir(mode=0o700)
    data = {"job_id": job_id, "unit": jobs.UNIT_PREFIX + job_id + ".service", "status": status,
            "created_at": time.time() - 30, "boot_id": jobs._boot_id(), "exit_code": None,
            "python": sys.executable, "worker_script": str(Path(worker.__file__).resolve()),
            "invocation_id": "a" * 32, "lifecycle_pending": True}
    jobs._atomic(folder / "status.json", data)
    return folder, data


def test_streaming_capture_caps_files_and_keeps_prefix(tmp_path):
    result = worker.capture([sys.executable, "-c", "import os; os.write(1,b'a'*2097152); os.write(2,b'b'*2097152)"], str(tmp_path), 5, tmp_path)
    assert result["outcome_status"] == "completed" and result["exit_code"] == 0
    for stream, char in (("stdout", b"a"), ("stderr", b"b")):
        raw = (tmp_path / (stream + ".txt")).read_bytes()
        assert len(raw) == jobs.MAX_SPOOL and raw == char * jobs.MAX_SPOOL
        assert result[stream + "_bytes_observed"] == 2097152
        assert (tmp_path / (stream + ".txt")).stat().st_mode & 0o777 == 0o600


def test_bounded_response_counts_escape_heavy_output(tmp_path, monkeypatch):
    folder, data = fixture_state(tmp_path, monkeypatch, "completed")
    data["stdout_bytes_observed"] = data["stderr_bytes_observed"] = jobs.MAX_SPOOL + 100
    for stream in ("stdout", "stderr"):
        (folder / (stream + ".txt")).write_bytes(b"\0" * jobs.MAX_SPOOL)
    response = jobs._with_output(folder, data)
    assert len(response["stdout"].encode()) == jobs.MAX_OUTPUT
    assert response["stdout_truncated"] and response["stdout_capture_truncated"]
    assert len(json.dumps(response, ensure_ascii=False).encode()) < 262144


def test_atomic_concurrent_writes_have_no_shared_temporary_name(tmp_path):
    target = tmp_path / "status.json"
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda n: jobs._atomic(target, {"writer": n, "body": str(n) * 4000}), range(80)))
    data = json.loads(target.read_text())
    assert data["body"] == str(data["writer"]) * 4000
    assert list(tmp_path.glob(".*.tmp")) == []
    assert target.stat().st_mode & 0o777 == 0o600


def test_reused_pid_is_not_a_live_worker(tmp_path, monkeypatch):
    folder, data = fixture_state(tmp_path, monkeypatch)
    current = jobs.process_identity(os.getpid())
    data["worker_identity"] = dict(current, start_ticks=current["start_ticks"] - 1)
    jobs._atomic(folder / "status.json", data)
    monkeypatch.setattr(jobs, "_unit_snapshot", lambda unit: {"LoadState": "not-found"})
    result = jobs.job_status(folder.name)
    assert result["status"] == "interrupted" and result["unit_quiescent"]
    assert jobs.process_identity(os.getpid())["start_ticks"] == current["start_ticks"]


def test_cancellation_refuses_reused_systemd_invocation(tmp_path, monkeypatch):
    folder, data = fixture_state(tmp_path, monkeypatch)
    monkeypatch.setattr(jobs, "_unit_snapshot", lambda unit: {"LoadState": "loaded", "Id": data["unit"], "Transient": "yes", "Description": "Aster MCP job " + folder.name, "InvocationID": "b" * 32, "ActiveState": "active"})
    calls = []
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: calls.append(a))
    result = jobs.cancel_job(folder.name)
    assert result["status"] == "recovery_required" and not result["cancelled"]
    assert calls == []


def test_no_terminal_label_while_cgroup_is_populated(tmp_path, monkeypatch):
    folder, data = fixture_state(tmp_path, monkeypatch, "finishing")
    data.update(cgroup="/system.slice/" + data["unit"], outcome_status="completed", exit_code=0)
    jobs._atomic(folder / "status.json", data)
    monkeypatch.setattr(jobs, "_unit_snapshot", lambda unit: {"LoadState": "not-found"})
    monkeypatch.setattr(jobs, "_cgroup_empty", lambda path: False)
    result = jobs.job_status(folder.name)
    assert result["status"] == "finishing" and result["lifecycle_pending"]
    assert not result.get("unit_quiescent")


def test_uncertain_unobserved_launch_does_not_settle_or_retry(tmp_path, monkeypatch):
    folder, data = fixture_state(tmp_path, monkeypatch, "launch_uncertain")
    data.pop("invocation_id")
    data["launch_uncertain"] = True
    jobs._atomic(folder / "status.json", data)
    monkeypatch.setattr(jobs, "_unit_snapshot", lambda unit: {"LoadState": "not-found"})
    result = jobs.job_status(folder.name)
    assert result["status"] == "recovery_required" and result["lifecycle_pending"]


def test_executor_validates_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "run_command", lambda *a: pytest.fail("unexpected launch"))
    with pytest.raises(ValueError):
        executor.execute({"operation": "run", "command": ["id"], "cwd": str(tmp_path), "timeout": True})
    with pytest.raises(ValueError):
        executor.execute({"operation": "run", "command": "id", "cwd": str(tmp_path)})


def test_missing_executable_is_structured_failure(tmp_path):
    result = worker.capture(["/nonexistent/aster-acceptance-command"], str(tmp_path), 1, tmp_path)
    assert result["outcome_status"] == "failed" and result["exit_code"] is None
    assert result["error"] == "execution failed"


@pytest.mark.skipif(os.geteuid() != 0, reason="root needed to exercise ordinary peer credentials")
def test_so_peercred_rejects_ordinary_user_even_on_permissive_test_socket(monkeypatch):
    # Permission intentionally relaxed on this isolated socket only: this test
    # independently exercises SO_PEERCRED, rather than Unix pathname denial.
    with tempfile.TemporaryDirectory(prefix="aster-peercred-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        path = root / "peer.sock"
        monkeypatch.setattr(executor, "AUDIT_PATH", str(root / "audit.jsonl"))
        with executor.Server(str(path), executor.Handler) as server:
            path.chmod(0o666)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            code = "import socket,json; s=socket.socket(socket.AF_UNIX); s.connect(%r); s.sendall(b'{\\\"operation\\\":\\\"run\\\"}\\n'); print(s.recv(4096).decode())" % str(path)
            try:
                result = subprocess.run(["/usr/bin/python3", "-I", "-c", code], cwd=str(root), capture_output=True, text=True, timeout=5, user=65534, group=65534, extra_groups=[])
                assert result.returncode == 0, result.stderr
                response = json.loads(result.stdout)
                assert response["status"] == "rejected" and response["error"] == "PermissionError"
            finally:
                server.shutdown()
                thread.join(2)


def test_reboot_reconciliation_clears_pending_state(tmp_path, monkeypatch):
    folder, data = fixture_state(tmp_path, monkeypatch)
    data.update(boot_id="earlier-boot", lifecycle_note="previous pending observation")
    jobs._atomic(folder / "status.json", data)
    monkeypatch.setattr(jobs, "_unit_snapshot", lambda unit: {"query_error": True})
    result = jobs.job_status(folder.name)
    assert result["status"] == "interrupted_by_reboot"
    assert result["unit_quiescent"] and not result["lifecycle_pending"]
    assert "lifecycle_note" not in result


@pytest.mark.skipif(os.geteuid() != 0, reason="root-local executor peer")
def test_non_object_request_gets_a_structured_rejection(tmp_path, monkeypatch):
    path = tmp_path / "executor.sock"
    monkeypatch.setattr(executor, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    with executor.Server(str(path), executor.Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(str(path))
                client.sendall(b'[]\n')
                response = json.loads(client.recv(4096))
            assert response["status"] == "rejected" and response["error"] == "ValueError"
            audit = json.loads((tmp_path / "audit.jsonl").read_text())
            assert audit["operation"] == "invalid" and audit["status"] == "rejected"
        finally:
            server.shutdown()
            thread.join(2)
