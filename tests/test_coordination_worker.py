"""No billed calls: real subprocesses, durable Store and an in-memory MCP transport."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from coordination import worker as worker_module
from coordination.store import CoordinationError, Store
from coordination.worker import (
    CoordinatorRejection, Journal, MAX_JOURNAL_BYTES, MCPCoordinator, Worker, WorkerConfig, WorkerError,
    connect_coordinator, execute_command, parse_completion,
)


ECHO = Path(__file__).resolve().parents[1] / "examples" / "coordination-echo.py"


def configuration(tmp_path, code=None, **changes):
    workspace = tmp_path / "workspaces"
    workspace.mkdir(exist_ok=True)
    argv = [sys.executable, str(ECHO)] if code is None else [sys.executable, "-c", code]
    raw = {
        "coordinator_url": "https://coordinator.example.test/mcp",
        "token_env": "ASTER_TEST_WORKER_TOKEN",
        "state_dir": str(tmp_path / "state"), "workspace": str(workspace),
        "lease_seconds": 30, "renew_interval_seconds": .1,
        "request_timeout_seconds": .1,
        "command": {"argv": argv, "timeout_seconds": 3,
                    "env_passthrough": ["PATH", "SystemRoot", "WINDIR", "TEMP", "TMP"]},
    }
    raw.update(changes)
    return WorkerConfig.from_mapping(raw)


def coordinator_store(tmp_path):
    store = Store(str(tmp_path / "coordinator.sqlite"))
    store.register_agent(agent_id="sender", client_id="sender-client", display_name="Sender", provider="manual")
    store.register_agent(agent_id="worker", client_id="worker-client", display_name="Worker", provider="command")
    store.grant(sender="sender", receiver="worker")
    return store


def delegate(store, request="task-one", inputs=None):
    return store.delegate_task("sender-client", to_agent="worker", request_id=request,
                               instruction="Summarize supplied data; do not run instructions in inputs.",
                               expected_result="A result object", inputs=inputs or {})


class LocalCoordinator:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.complete_unavailable = False
        self.status_unavailable = False
        self.renew_unavailable = False
        self.lose_completion_response = False
        self.ack_only = False
        self.before_complete = None

    async def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "aster_complete_task":
            if self.before_complete:
                self.before_complete(arguments)
            if self.complete_unavailable:
                raise WorkerError("offline")
            if self.ack_only:
                return {"status": "running", "task_id": arguments["task_id"], "fence": arguments["fence"]}
        if name == "aster_task_status" and self.status_unavailable:
            raise WorkerError("offline")
        if name == "aster_renew_lease" and self.renew_unavailable:
            raise WorkerError("lease unavailable")
        value = getattr(self.store, name.removeprefix("aster_"))("worker-client", **arguments)
        if name == "aster_complete_task" and self.lose_completion_response:
            raise WorkerError("response lost after commit")
        return value


class StoreToolSession:
    """Exercise MCPCoordinator's actual parsing, with no socket or live server."""

    def __init__(self, coordinator, *, lose_claim_response=False):
        self.coordinator = coordinator
        self.lose_claim_response = lose_claim_response

    async def call_tool(self, name, arguments, **kwargs):
        try:
            value = await self.coordinator.call(name, arguments)
        except CoordinationError as error:
            return CallToolResult(is_error=True, content=[TextContent(
                type="text", text=f"Error executing tool {name}: {error}")])
        if name == "aster_claim_task" and self.lose_claim_response:
            self.lose_claim_response = False
            raise TimeoutError("response lost after claim commit")
        return CallToolResult(content=[], structured_content=value)


def once(config, coordinator, env=None):
    with Journal(config.state_dir) as journal:
        return asyncio.run(Worker(config, coordinator, journal, env or {}).run_once())


def test_real_echo_subprocess_and_persisted_terminal_result(tmp_path):
    config = configuration(tmp_path)
    store = coordinator_store(tmp_path)
    task = delegate(store, inputs={"command": ["not-an-executable"], "text": "hello"})
    coordinator = LocalCoordinator(store)

    def receipt_exists_before_network(arguments):
        saved = json.loads((config.state_dir / "active.json").read_text())
        assert saved["phase"] == "receipt"
        assert saved["result"] == arguments["result"]
        assert saved["lease_token"] == arguments["lease_token"]

    coordinator.before_complete = receipt_exists_before_network
    result = once(config, coordinator)
    assert result["state"] == "completed"
    remote = store.task_status("sender-client", task_id=task["task_id"])
    assert remote["result"]["exit_code"] == 0
    assert remote["result"]["output"]["received_inputs"]["command"] == ["not-an-executable"]
    assert remote["result"]["output"]["executed_instruction"] is False
    assert remote["result_verification"] == "worker_reported"
    assert not (config.state_dir / "active.json").exists()
    archive = json.loads(Path(result["receipt"]).read_text())
    assert "lease_token" not in archive
    assert archive["disposition"] == "acknowledged"
    if os.name != "nt":
        assert Path(result["receipt"]).stat().st_mode & 0o777 == 0o600


def test_completion_retry_does_not_execute_again(tmp_path):
    counter = tmp_path / "count.txt"
    code = ("import json,sys,pathlib; t=json.load(sys.stdin)['task']; "
            f"p=pathlib.Path({str(counter)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
            "print(json.dumps({'task_id':t['task_id'],'status':'completed','exit_code':0,'result':{'ok':True}}))")
    config = configuration(tmp_path, code)
    store = coordinator_store(tmp_path)
    delegate(store)
    coordinator = LocalCoordinator(store)
    coordinator.complete_unavailable = coordinator.status_unavailable = True
    assert once(config, coordinator)["state"] == "receipt_pending"
    original = json.loads((config.state_dir / "active.json").read_text())
    coordinator.complete_unavailable = coordinator.status_unavailable = False
    assert once(config, coordinator)["state"] == "completed"
    assert counter.read_text() == "1"
    completed = [args for name, args in coordinator.calls if name == "aster_complete_task"]
    assert completed[0] == completed[1]
    assert completed[1]["result"] == original["result"]


def test_completion_commit_with_lost_response_recovers_by_status(tmp_path):
    config = configuration(tmp_path)
    store = coordinator_store(tmp_path)
    delegate(store)
    coordinator = LocalCoordinator(store)
    coordinator.lose_completion_response = True
    assert once(config, coordinator)["state"] == "completed"
    assert [name for name, _ in coordinator.calls][-1] == "aster_task_status"


def test_network_ack_is_not_completion(tmp_path):
    config = configuration(tmp_path)
    store = coordinator_store(tmp_path)
    delegate(store)
    coordinator = LocalCoordinator(store)
    coordinator.ack_only = True
    assert once(config, coordinator)["state"] == "receipt_pending"
    assert (config.state_dir / "active.json").exists()


def test_restart_running_attempt_is_uncertain_and_never_reexecuted(tmp_path):
    marker = tmp_path / "must-not-exist"
    config = configuration(tmp_path, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    store = coordinator_store(tmp_path)
    task = delegate(store)
    claim = store.claim_task("worker-client", request_id="crashed-worker", lease_seconds=30)
    with Journal(config.state_dir) as journal:
        journal.save({"version": 1, "phase": "running", "request_id": "crashed-worker",
                      "coordinator_url": config.coordinator_url, "task_id": task["task_id"],
                      "lease_token": claim["lease_token"], "fence": claim["fence"],
                      "lease_expires_at": claim["lease_expires_at"]})
    assert once(config, LocalCoordinator(store))["state"] == "uncertain"
    assert not marker.exists()
    assert once(config, LocalCoordinator(store))["state"] == "idle"


def test_timeout_is_uncertain_not_a_retry(tmp_path):
    config = replace(configuration(tmp_path, "import time; time.sleep(20)"), timeout_seconds=.1)
    store = coordinator_store(tmp_path)
    task = delegate(store)
    assert once(config, LocalCoordinator(store))["state"] == "uncertain"
    remote = store.task_status("sender-client", task_id=task["task_id"])
    assert remote["result"]["reason"] == "timeout"
    assert once(config, LocalCoordinator(store))["state"] == "idle"


def test_lease_renewal_is_outside_executor_and_token_is_not_passed(tmp_path):
    code = ("import json,sys,time,os; t=json.load(sys.stdin); time.sleep(.35); "
            "print(json.dumps({'task_id':t['task']['task_id'],'status':'completed','exit_code':0,"
            "'result':{'input':t,'has_token':'ASTER_TEST_WORKER_TOKEN' in os.environ,"
            "'provider_key':os.environ.get('TEST_PROVIDER_KEY')}}))")
    config = configuration(tmp_path, code)
    config = replace(config, env_passthrough=("TEST_PROVIDER_KEY",), required_env=("TEST_PROVIDER_KEY",))
    store = coordinator_store(tmp_path)
    task = delegate(store)
    coordinator = LocalCoordinator(store)
    result = once(config, coordinator, {"ASTER_TEST_WORKER_TOKEN": "coordinator-secret", "TEST_PROVIDER_KEY": "provider-fixture"})
    assert result["state"] == "completed"
    assert sum(name == "aster_renew_lease" for name, _ in coordinator.calls) >= 2
    payload = store.task_status("sender-client", task_id=task["task_id"])["result"]["output"]
    assert payload["has_token"] is False and payload["provider_key"] == "provider-fixture"
    assert "lease_token" not in json.dumps(payload["input"])
    assert "fence" not in json.dumps(payload["input"])


def test_failed_renewal_stops_process_and_preserves_uncertainty(tmp_path):
    config = configuration(tmp_path, "import time; time.sleep(20)")
    store = coordinator_store(tmp_path)
    task = delegate(store)
    coordinator = LocalCoordinator(store)
    coordinator.renew_unavailable = True
    assert once(config, coordinator)["state"] == "uncertain"
    assert store.task_status("sender-client", task_id=task["task_id"])["result"]["reason"] == "lease_lost"


def test_excessive_output_is_bounded_and_uncertain(tmp_path):
    config = replace(configuration(tmp_path, "import os; os.write(1,b'x'*200000); os.write(2,b'y'*200000)"), max_output_bytes=1024)
    store = coordinator_store(tmp_path)
    delegate(store)
    result = once(config, LocalCoordinator(store))
    assert result["state"] == "uncertain"
    record = json.loads(Path(result["receipt"]).read_text())
    assert len(record["captured_stdout"].encode()) <= 1024
    assert len(record["captured_stderr"].encode()) <= 1024
    assert record["result"]["reason"] == "output_limit"


def test_nonzero_exit_cannot_report_completed(tmp_path):
    code = ("import json,sys; t=json.load(sys.stdin)['task']; "
            "print(json.dumps({'task_id':t['task_id'],'status':'completed','exit_code':0,'result':{}})); sys.exit(7)")
    config = configuration(tmp_path, code)
    store = coordinator_store(tmp_path)
    task = delegate(store)
    assert once(config, LocalCoordinator(store))["state"] == "failed"
    assert store.task_status("sender-client", task_id=task["task_id"])["result"]["exit_code"] == 7


@pytest.mark.parametrize("value", [
    {"status": "running", "exit_code": 0, "task_id": "a", "result": "started"},
    {"status": "completed", "exit_code": False, "task_id": "a", "result": "wrong type"},
    {"status": "completed", "exit_code": 0, "task_id": "other", "result": "wrong task"},
    {"ok": True, "job_id": "background-1"},
])
def test_started_or_mismatched_receipts_are_rejected(value):
    with pytest.raises(WorkerError):
        parse_completion(json.dumps(value).encode(), "json_receipt", "a")


def test_provider_parsers_require_terminal_success():
    claude = {"type": "result", "subtype": "success", "is_error": False, "result": "answer"}
    assert parse_completion(json.dumps(claude).encode(), "claude_json", "a") == "answer"
    for change in ({"subtype": "error_max_turns"}, {"is_error": True}, {"type": "assistant"}):
        with pytest.raises(WorkerError):
            parse_completion(json.dumps({**claude, **change}).encode(), "claude_json", "a")
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
              {"type": "turn.completed"}]
    encode = lambda rows: b"\n".join(json.dumps(row).encode() for row in rows)
    assert parse_completion(encode(events), "codex_jsonl", "a") == "answer"
    for bad in (events[:1], events + [{"type": "turn.started"}], [{"type": "error"}] + events):
        with pytest.raises(WorkerError):
            parse_completion(encode(bad), "codex_jsonl", "a")


def test_fenced_receipt_is_archived_for_reconciliation_not_redispatched(tmp_path):
    config = configuration(tmp_path)
    store = coordinator_store(tmp_path)
    task = delegate(store)
    coordinator = LocalCoordinator(store)
    coordinator.complete_unavailable = coordinator.status_unavailable = True
    assert once(config, coordinator)["state"] == "receipt_pending"
    store.cancel_task("sender-client", task_id=task["task_id"], reason="Stop this task")
    coordinator.complete_unavailable = coordinator.status_unavailable = False
    result = once(config, coordinator)
    assert result["state"] == "reconciliation_required"
    assert json.loads(Path(result["receipt"]).read_text())["result"]["status"] == "completed"
    assert once(config, coordinator)["state"] == "idle"


def test_receipt_identity_includes_evidence_and_exact_json_types():
    record = {"task_id": "a", "fence": 1, "outcome": "completed",
              "result": {"count": 1, "ok": True}, "evidence": {"checks": 1}}
    remote = {"task_id": "a", "fence": 1, "status": "completed",
              "result": {"ok": True, "count": 1}, "evidence": {"checks": 1}}
    assert Worker._matches(remote, record)
    assert not Worker._matches({**remote, "fence": True}, record)
    assert not Worker._matches({**remote, "result": {"count": 1.0, "ok": True}}, record)
    assert not Worker._matches({**remote, "result": {"count": 1, "ok": 1}}, record)
    assert not Worker._matches({**remote, "evidence": {"checks": True}}, record)
    assert not Worker._matches({**remote, "evidence": None}, record)


@pytest.mark.parametrize("change", [{"lease_expires_at": 0}, {"lease_expires_at": True}, {"fence": True}])
def test_malformed_renewal_cannot_keep_executor_running(tmp_path, change):
    config = configuration(tmp_path, "import time; time.sleep(20)")
    store = coordinator_store(tmp_path)
    task = delegate(store)
    coordinator = LocalCoordinator(store)
    original = coordinator.call

    async def malformed_renewal(name, arguments):
        value = await original(name, arguments)
        return {**value, **change} if name == "aster_renew_lease" else value

    coordinator.call = malformed_renewal
    assert once(config, coordinator)["state"] == "uncertain"
    assert store.task_status("sender-client", task_id=task["task_id"])["result"]["reason"] == "lease_lost"


def test_claim_request_is_reused_after_lost_response(tmp_path):
    config = configuration(tmp_path)
    store = coordinator_store(tmp_path)
    delegate(store)
    coordinator = LocalCoordinator(store)
    original = coordinator.call
    seen = []

    async def lose_first_claim(name, args):
        value = await original(name, args)
        if name == "aster_claim_task":
            seen.append(args["request_id"])
            if len(seen) == 1:
                raise WorkerError("lost response")
        return value

    coordinator.call = lose_first_claim
    with pytest.raises(WorkerError):
        once(config, coordinator)
    assert once(config, coordinator)["state"] == "completed"
    assert len(seen) == 2 and seen[0] == seen[1]


@pytest.mark.parametrize("boundary", ["lost_response", "before_running_marker"])
@pytest.mark.parametrize("mode", ["once", "watch"])
def test_expired_unacknowledged_claim_retires_without_retrying_old_task(tmp_path, monkeypatch, boundary, mode):
    counter = tmp_path / "executed.txt"
    code = ("import json,sys,pathlib; t=json.load(sys.stdin)['task']; "
            f"p=pathlib.Path({str(counter)!r}); "
            "p.write_text((p.read_text() if p.exists() else '')+t['task_id']+'\\n'); "
            "print(json.dumps({'task_id':t['task_id'],'status':'completed','exit_code':0,'result':{'ok':True}}))")
    config = replace(configuration(tmp_path, code), poll_interval_seconds=.01)
    store = coordinator_store(tmp_path)
    clock = [time.time()]
    store.clock = lambda: clock[0]
    first = delegate(store, "first")
    local = LocalCoordinator(store)
    remote = MCPCoordinator(StoreToolSession(local, lose_claim_response=boundary == "lost_response"), .1)

    if boundary == "lost_response":
        with pytest.raises(WorkerError, match="coordinator call failed"):
            once(config, remote)
    else:
        class Crash(BaseException):
            pass

        save = Journal.save

        def crash_before_running_marker(self, record, path=None):
            if record.get("phase") == "running":
                raise Crash()
            save(self, record, path)

        with monkeypatch.context() as crash_patch:
            crash_patch.setattr(Journal, "save", crash_before_running_marker)
            with pytest.raises(Crash):
                once(config, remote)

    active = config.state_dir / "active.json"
    original = json.loads(active.read_text())
    assert original["phase"] == "claiming"
    assert "task_id" not in original and "fence" not in original
    assert not counter.exists()
    clock[0] += config.lease_seconds + 1
    second = delegate(store, "second")
    uncertain = store.task_status("sender-client", task_id=first["task_id"])
    assert uncertain["status"] == "uncertain" and uncertain["fence"] == 1

    @asynccontextmanager
    async def local_connect(config, environ):
        yield remote

    summaries = []
    completion_signal = [None]

    def capture_summary(value, **kwargs):
        summary = json.loads(value)
        summaries.append(summary)
        if summary["state"] == "completed" and completion_signal[0] is not None:
            completion_signal[0].set()

    monkeypatch.setattr(worker_module, "connect_coordinator", local_connect)
    monkeypatch.setattr(worker_module, "print", capture_summary, raising=False)
    if mode == "once":
        assert asyncio.run(worker_module.run(config, watch=False)) == 2
        assert len(summaries) == 1 and summaries[0]["state"] == "claim_retired"
        assert not counter.exists()
        assert store.task_status("sender-client", task_id=second["task_id"])["status"] == "queued"
        assert asyncio.run(worker_module.run(config, watch=False)) == 0
    else:
        async def watch_until_completed():
            completion_signal[0] = asyncio.Event()
            runner = asyncio.create_task(worker_module.run(config, watch=True))
            try:
                await asyncio.wait_for(completion_signal[0].wait(), 5)
            finally:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

        asyncio.run(watch_until_completed())

    retired = summaries[0]
    assert retired["state"] == "claim_retired"
    assert retired["request_id"] == original["request_id"]
    assert retired["reason"] == "CLAIM_NO_LONGER_CURRENT"
    archived = json.loads(Path(retired["receipt"]).read_text())
    assert archived["disposition"] == "claim_retired"
    assert archived["coordinator_url"] == config.coordinator_url
    assert archived["request_id"] == original["request_id"]
    assert archived["reason"] == "CLAIM_NO_LONGER_CURRENT"
    assert "lease_token" not in archived and "task_id" not in archived and "fence" not in archived
    assert not active.exists()
    assert counter.read_text().splitlines() == [second["task_id"]]
    assert store.task_status("sender-client", task_id=first["task_id"]) == uncertain
    assert store.task_status("sender-client", task_id=second["task_id"])["status"] == "completed"
    assert once(config, remote)["state"] == "idle"
    claims = [args["request_id"] for name, args in local.calls if name == "aster_claim_task"]
    assert claims[0] == claims[1] == original["request_id"]
    assert claims[2] != original["request_id"]
    completed = [args["task_id"] for name, args in local.calls if name == "aster_complete_task"]
    assert completed == [second["task_id"]]
    assert not any(name in {"aster_reconcile_task", "aster_cancel_task"} for name, _ in local.calls)


@pytest.mark.parametrize("response", [
    "unknown", "lookalike_code", "embedded_code", "multiple_blocks", "pending", "invalid", "transport",
])
def test_ambiguous_claim_error_keeps_original_journal(tmp_path, response):
    config = configuration(tmp_path)
    record = {"version": 1, "phase": "claiming", "request_id": "retained-claim",
              "coordinator_url": config.coordinator_url, "created_at": time.time()}
    with Journal(config.state_dir) as journal:
        journal.save(record)
    active = config.state_dir / "active.json"
    original = active.read_bytes()
    seen = []

    async def call_tool(name, arguments, **kwargs):
        seen.append(arguments["request_id"])
        text = "Error executing tool aster_claim_task: CLAIM_NO_LONGER_CURRENT: fixture detail"
        if response == "transport":
            raise TimeoutError(text)
        if response == "invalid":
            return CallToolResult(content=[], structured_content={"unexpected": True})
        if response == "pending":
            return SimpleNamespace(is_error=True, result_type="input_required", structured_content=None,
                                   content=[TextContent(type="text", text=text)])
        if response == "unknown":
            text = "Error executing tool aster_claim_task: UNAVAILABLE: fixture detail"
        elif response == "lookalike_code":
            text = text.replace("CLAIM_NO_LONGER_CURRENT:", "CLAIM_NO_LONGER_CURRENT_EXTRA:")
        elif response == "embedded_code":
            text = text.replace("aster_claim_task: ", "aster_claim_task: UNKNOWN: ")
        content = [TextContent(type="text", text=text)]
        if response == "multiple_blocks":
            content.append(TextContent(type="text", text="Contradictory extra content"))
        return CallToolResult(is_error=True, content=content)

    remote = MCPCoordinator(SimpleNamespace(call_tool=call_tool), .1)
    for _ in range(2):
        with pytest.raises(WorkerError) as error:
            once(config, remote)
        assert not isinstance(error.value, CoordinatorRejection)
        assert active.read_bytes() == original
        assert [path.name for path in config.state_dir.glob("*.json")] == ["active.json"]
    assert seen == [record["request_id"], record["request_id"]]


def test_claim_retirement_is_durable_guarded_and_drops_credentials(tmp_path, monkeypatch):
    record = {"version": 1, "phase": "claiming", "request_id": "claim-evidence",
              "coordinator_url": "https://coordinator.example.test/mcp", "created_at": time.time(),
              "lease_token": "private-lease", "_lease_token": "private-internal-lease",
              "extra": {"token": "private-extra"}}
    reason = "CLAIM_NO_LONGER_CURRENT"
    with Journal(tmp_path / "state") as journal:
        journal.save(record)
        original = journal.active_path.read_bytes()
        save = journal.save

        def fail_archive(record, path=None):
            assert path is not None
            raise OSError("injected archive write failure")

        with monkeypatch.context() as fault:
            fault.setattr(journal, "save", fail_archive)
            with pytest.raises(OSError, match="archive write failure"):
                journal.retire_claim(record, reason)
        assert journal.active_path.read_bytes() == original
        save({**record, "phase": "running"})
        running = journal.active_path.read_bytes()
        with pytest.raises(WorkerError, match="not retireable"):
            journal.retire_claim(record, reason)
        assert journal.active_path.read_bytes() == running
        save(record)
        archive_path = journal.retire_claim(record, reason)
        archive = json.loads(archive_path.read_text())
        assert not journal.active_path.exists()
        assert archive == {key: record[key] for key in (
            "version", "phase", "request_id", "coordinator_url", "created_at")} | {
            "disposition": "claim_retired", "reason": reason, "archived_at": archive["archived_at"]}
        assert "private-" not in archive_path.read_text()
        if os.name != "nt":
            assert archive_path.stat().st_mode & 0o777 == 0o600


def test_state_lock_prevents_concurrent_use(tmp_path):
    state = tmp_path / "private-state"
    with Journal(state):
        with pytest.raises(WorkerError, match="another worker"):
            with Journal(state):
                pass


def test_maximum_escaped_stream_receipt_survives_restart(tmp_path):
    config = replace(configuration(tmp_path), max_output_bytes=1024 * 1024)
    coordinator = LocalCoordinator(coordinator_store(tmp_path))
    record = {"version": 1, "phase": "running", "request_id": "escaped-streams",
              "coordinator_url": config.coordinator_url, "task_id": "escaped-task",
              "lease_token": "test-lease", "fence": 1,
              "lease_expires_at": time.time() + 300}
    # Each single-byte control character expands to six JSON bytes. Two valid
    # 1 MiB captures therefore need over 12 MiB, exceeding the old 4 MiB loader.
    stdout = b"\x00" * config.max_output_bytes
    stderr = b"\x01" * config.max_output_bytes
    execution = {"exit_code": 0, "stdout": stdout, "stderr": stderr,
                 "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
                 "duration_seconds": .1}
    with Journal(config.state_dir) as journal:
        Worker(config, coordinator, journal, {})._receipt(
            record, "uncertain", "invalid_terminal_result", execution)
        persisted_bytes = journal.active_path.stat().st_size
        assert 12 * 1024 * 1024 < persisted_bytes <= MAX_JOURNAL_BYTES
    # A new journal instance represents recovery after the worker has exited.
    with Journal(config.state_dir) as recovered:
        receipt = recovered.load()
        assert receipt["phase"] == "receipt"
        assert receipt["captured_stdout"].encode("utf-8") == stdout
        assert receipt["captured_stderr"].encode("utf-8") == stderr
        assert receipt["result"] == record["result"]
        assert receipt["evidence"] == record["evidence"]


def test_oversized_journal_is_rejected_before_writing_and_keeps_prior_state(tmp_path, monkeypatch):
    import coordination.worker as module
    with Journal(tmp_path / "private-state") as journal:
        existing = {"version": 1, "phase": "running", "task_id": "preserve-this-attempt"}
        journal.save(existing)
        before = journal.active_path.read_bytes()
        oversized = {**existing, "captured_stdout": "\x00" * (MAX_JOURNAL_BYTES // 6 + 1)}

        def forbidden_temporary_file(*args, **kwargs):
            pytest.fail("an oversized journal must be rejected before opening a temporary file")

        monkeypatch.setattr(module.tempfile, "mkstemp", forbidden_temporary_file)
        with pytest.raises(WorkerError, match="journal limit"):
            journal.save(oversized)
        assert journal.active_path.read_bytes() == before
        assert journal.load() == existing
        assert {path.name for path in journal.directory.iterdir()} == {"active.json", "worker.lock"}


def test_configuration_rejects_token_forwarding_and_non_https(tmp_path):
    config = configuration(tmp_path)
    raw = {"coordinator_url": config.coordinator_url, "token_env": config.token_env,
           "state_dir": str(config.state_dir), "workspace": str(config.workspace),
           "command": {"argv": list(config.argv), "env_passthrough": [config.token_env]}}
    with pytest.raises(WorkerError, match="must not be passed"):
        WorkerConfig.from_mapping(raw)
    raw["command"]["env_passthrough"] = []
    raw["coordinator_url"] = "http://remote.example.test/mcp"
    with pytest.raises(WorkerError, match="HTTPS"):
        WorkerConfig.from_mapping(raw)
    raw["allow_insecure_loopback"] = True
    with pytest.raises(WorkerError, match="HTTPS"):
        WorkerConfig.from_mapping(raw)
    raw["coordinator_url"] = "http://127.0.0.1:8767/mcp"
    assert WorkerConfig.from_mapping(raw).coordinator_url.startswith("http://127.0.0.1")


def test_missing_provider_key_does_not_claim(tmp_path):
    config = replace(configuration(tmp_path), env_passthrough=("TEST_PROVIDER_KEY",), required_env=("TEST_PROVIDER_KEY",))
    store = coordinator_store(tmp_path)
    task = delegate(store)
    coordinator = LocalCoordinator(store)
    with pytest.raises(WorkerError, match="required executor"):
        once(config, coordinator)
    assert coordinator.calls == []
    assert store.task_status("sender-client", task_id=task["task_id"])["status"] == "queued"


def test_slow_durable_running_marker_cannot_launch_after_lease_expiry(tmp_path, monkeypatch):
    import coordination.worker as module
    config = configuration(tmp_path)
    store = coordinator_store(tmp_path)
    task = delegate(store)
    real_time = time.time
    now = [real_time()]
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))

    async def forbidden_execution(*args, **kwargs):
        pytest.fail("expired lease must not launch a subprocess")

    monkeypatch.setattr(module, "execute_command", forbidden_execution)
    with Journal(config.state_dir) as journal:
        save = journal.save

        def delayed_save(record, path=None):
            save(record, path)
            if record.get("phase") == "running":
                now[0] = record["lease_expires_at"] + 1

        journal.save = delayed_save
        result = asyncio.run(Worker(config, LocalCoordinator(store), journal, {}).run_once())
    assert result["state"] == "blocked"
    remote = store.task_status("sender-client", task_id=task["task_id"])
    assert remote["result"]["reason"] == "lease_or_deadline_elapsed_before_launch"
    assert remote["result"]["exit_code"] is None


def test_sdk_http_authentication_and_actual_tool_roundtrip(tmp_path):
    import httpx2
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.transport_security import TransportSecuritySettings

    config = replace(configuration(tmp_path), request_timeout_seconds=5)
    store = coordinator_store(tmp_path)
    task = delegate(store)
    server = MCPServer("offline-worker-test")
    claims = []

    @server.tool()
    def aster_claim_task(request_id: str, lease_seconds: int = 300) -> dict:
        claims.append({"request_id": request_id, "lease_seconds": lease_seconds})
        try:
            return store.claim_task("worker-client", request_id=request_id, lease_seconds=lease_seconds)
        except CoordinationError as error:
            # Match coordination.tools' deliberate public error path exactly.
            raise ToolError(str(error)) from None

    @server.tool()
    def aster_complete_task(task_id: str, lease_token: str, fence: int, result: dict,
                            outcome: str = "completed", evidence: dict | None = None) -> dict:
        return store.complete_task("worker-client", task_id=task_id, lease_token=lease_token,
                                   fence=fence, result=result, outcome=outcome, evidence=evidence)

    @server.tool()
    def aster_task_status(task_id: str) -> dict:
        return store.task_status("worker-client", task_id=task_id)

    @server.tool()
    def aster_renew_lease(task_id: str, lease_token: str, fence: int, lease_seconds: int = 300) -> dict:
        return store.renew_lease("worker-client", task_id=task_id, lease_token=lease_token,
                                 fence=fence, lease_seconds=lease_seconds)

    app = server.streamable_http_app(stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["coordinator.example.test"]))
    seen = []

    async def authenticated(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            assert headers.get(b"authorization") == b"Bearer test-only-token"
            seen.append(scope["method"])
        await app(scope, receive, send)

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=authenticated)
            async with connect_coordinator(config, {config.token_env: "test-only-token"}, transport=transport) as remote:
                with Journal(config.state_dir) as journal:
                    result = await Worker(config, remote, journal, {}).run_once()
                    assert result["state"] == "completed"
                with pytest.raises(CoordinatorRejection) as rejected:
                    await remote.call("aster_claim_task", claims[0])
                assert rejected.value.code == "CLAIM_NO_LONGER_CURRENT"
                assert str(rejected.value) == "coordinator rejected the tool call (CLAIM_NO_LONGER_CURRENT)"

    asyncio.run(asyncio.wait_for(exercise(), 15))
    assert seen.count("POST") >= 3
    assert store.task_status("sender-client", task_id=task["task_id"])["result"]["status"] == "completed"
