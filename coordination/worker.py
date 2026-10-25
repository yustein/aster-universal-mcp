"""Explicit, supervised pull worker. Run with ``python -m coordination.worker``.

The operator chooses a fixed executable/argv. Task data travels only on stdin;
the executor never receives the coordinator bearer token or the task lease.
The private local journal is part of the execution protocol, not a cache.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit
import uuid

MAX_JOURNAL_BYTES = 16 * 1024 * 1024


class WorkerError(Exception):
    """A safe diagnostic; do not include tokens, task text or provider stderr."""


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def _loads(value: str | bytes) -> Any:
    return json.loads(value, parse_constant=_reject_constant)


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerError(f"{name} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise WorkerError(f"{name} is outside its allowed range")
    return float(value)


def _absolute(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise WorkerError(f"{name} must be an absolute path")
    return Path(value)


@dataclass(frozen=True)
class WorkerConfig:
    coordinator_url: str
    token_env: str
    state_dir: Path
    workspace: Path
    argv: tuple[str, ...]
    output_format: str = "json_receipt"
    env_passthrough: tuple[str, ...] = ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG")
    required_env: tuple[str, ...] = ()
    timeout_seconds: float = 240
    lease_seconds: int = 300
    renew_interval_seconds: float = 60
    request_timeout_seconds: float = 20
    poll_interval_seconds: float = 5
    max_output_bytes: int = 65536
    max_input_bytes: int = 131072
    workspace_mode: str = "isolated"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WorkerConfig":
        allowed = {"coordinator_url", "token_env", "state_dir", "workspace", "command",
                   "lease_seconds", "renew_interval_seconds", "request_timeout_seconds",
                   "poll_interval_seconds", "allow_insecure_loopback"}
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise WorkerError("unknown worker configuration field")
        command = raw.get("command")
        command_keys = {"argv", "output_format", "env_passthrough", "required_env",
                        "timeout_seconds", "max_output_bytes", "max_input_bytes", "workspace_mode"}
        if not isinstance(command, dict) or set(command) - command_keys:
            raise WorkerError("invalid command configuration")
        url = raw.get("coordinator_url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            port = parsed.port if parsed else None
        except ValueError:
            raise WorkerError("invalid coordinator URL") from None
        insecure = raw.get("allow_insecure_loopback", False)
        if not isinstance(insecure, bool):
            raise WorkerError("allow_insecure_loopback must be boolean")
        if (not parsed or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or port == 0
                or (parsed.scheme != "https" and not (
                    insecure and parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise WorkerError("coordinator URL must be HTTPS; explicit loopback HTTP is for local tests")
        token_env = raw.get("token_env", "ASTER_COORDINATION_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise WorkerError("invalid token_env name")
        argv = command.get("argv")
        if (not isinstance(argv, list) or not argv or len(argv) > 128
                or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 16384 for arg in argv)):
            raise WorkerError("command.argv must be a bounded array of literal strings")
        _absolute(argv[0], "command.argv[0]")
        output_format = command.get("output_format", "json_receipt")
        if output_format not in {"json_receipt", "claude_json", "codex_jsonl"}:
            raise WorkerError("unsupported command.output_format")
        env_names = command.get("env_passthrough", list(cls.env_passthrough))
        required_env = command.get("required_env", [])
        for names in (env_names, required_env):
            if (not isinstance(names, list) or any(not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names)):
                raise WorkerError("environment lists must contain variable names only")
        if token_env.casefold() in {name.casefold() for name in env_names}:
            raise WorkerError("the coordinator token must not be passed to the executor")
        if not set(required_env) <= set(env_names):
            raise WorkerError("required_env must be included in env_passthrough")
        lease = _number(raw.get("lease_seconds", 300), "lease_seconds", 30, 3600)
        if not lease.is_integer():
            raise WorkerError("lease_seconds must be an integer")
        renew = _number(raw.get("renew_interval_seconds", 60), "renew_interval_seconds", 0.1, 600)
        request_timeout = _number(raw.get("request_timeout_seconds", 20), "request_timeout_seconds", 0.1, 120)
        if renew + request_timeout >= lease / 2:
            raise WorkerError("renew interval plus request timeout must be less than half the lease")
        mode = command.get("workspace_mode", "isolated")
        if mode not in {"isolated", "fixed"}:
            raise WorkerError("workspace_mode must be isolated or fixed")
        limits = {}
        for name, default, minimum, maximum in (
                ("max_output_bytes", 65536, 1024, 1048576),
                ("max_input_bytes", 131072, 1024, 1048576)):
            number = _number(command.get(name, default), name, minimum, maximum)
            if not number.is_integer():
                raise WorkerError(f"{name} must be an integer")
            limits[name] = int(number)
        return cls(
            coordinator_url=url, token_env=token_env,
            state_dir=_absolute(raw.get("state_dir"), "state_dir"),
            workspace=_absolute(raw.get("workspace"), "workspace"),
            argv=tuple(argv), output_format=output_format,
            env_passthrough=tuple(env_names), required_env=tuple(required_env),
            timeout_seconds=_number(command.get("timeout_seconds", 240), "timeout_seconds", 0.1, 86400),
            lease_seconds=int(lease), renew_interval_seconds=renew,
            request_timeout_seconds=request_timeout,
            poll_interval_seconds=_number(raw.get("poll_interval_seconds", 5), "poll_interval_seconds", 1, 3600),
            workspace_mode=mode, **limits)

    @classmethod
    def load(cls, path: str | Path) -> "WorkerConfig":
        try:
            data = Path(path).read_bytes()
            if len(data) > 65536:
                raise WorkerError("worker configuration is too large")
            return cls.from_mapping(_loads(data))
        except (OSError, ValueError, TypeError) as exc:
            raise WorkerError("cannot read a valid worker configuration") from exc

    def command_environment(self, environ: Mapping[str, str]) -> dict[str, str]:
        if any(not environ.get(name) for name in self.required_env):
            raise WorkerError("a required executor environment variable is missing")
        return {name: environ[name] for name in self.env_passthrough
                if name in environ and name.casefold() != self.token_env.casefold()}

    def preflight(self, environ: Mapping[str, str]) -> None:
        if not Path(self.argv[0]).is_file() or not os.access(self.argv[0], os.X_OK):
            raise WorkerError("configured executor is not an executable file")
        if not self.workspace.is_dir():
            raise WorkerError("configured workspace must already exist")
        self.command_environment(environ)


class Journal:
    """One locked worker journal, with atomic/fsynced transitions and receipts."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.active_path = directory / "active.json"
        self._lock: Any = None

    def __enter__(self) -> "Journal":
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise WorkerError("worker state directory must not be a symlink")
        if os.name != "nt" and stat.S_IMODE(self.directory.stat().st_mode) & 0o077:
            raise WorkerError("worker state directory must be private (mode 0700)")
        lock_path = self.directory / "worker.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        self._lock = os.fdopen(os.open(lock_path, flags, 0o600), "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lock.write(b"\0")
                self._lock.flush()
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            self._lock = None
            raise WorkerError("another worker owns this state directory") from None
        return self

    def __exit__(self, *args: Any) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def _sync_directory(self) -> None:
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def save(self, record: dict[str, Any], path: Path | None = None) -> None:
        encoded = _json(record)
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise WorkerError("worker receipt exceeds its journal limit; preserve execution state for review")
        descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path or self.active_path)
            self._sync_directory()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self) -> dict[str, Any] | None:
        if not self.active_path.exists():
            return None
        try:
            if self.active_path.is_symlink() or self.active_path.stat().st_size > MAX_JOURNAL_BYTES:
                raise ValueError("unsafe journal")
            record = _loads(self.active_path.read_bytes())
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ValueError("unknown journal version")
            return record
        except (OSError, ValueError, TypeError) as exc:
            raise WorkerError("worker journal is invalid; preserve it for operator reconciliation") from exc

    def clear(self) -> None:
        self.active_path.unlink(missing_ok=True)
        self._sync_directory()

    def archive(self, record: dict[str, Any], disposition: str) -> Path:
        archive = dict(record, disposition=disposition, archived_at=time.time())
        archive.pop("lease_token", None)
        identity = f"{record['task_id']}:{record['fence']}:{record['request_id']}"
        path = self.directory / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        self.save(archive, path)
        self.clear()
        return path


class Coordinator(Protocol):
    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class MCPCoordinator:
    def __init__(self, session: Any, timeout: float):
        self.session, self.timeout = session, timeout

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self.session.call_tool(name, arguments, read_timeout_seconds=self.timeout)
            if getattr(result, "is_error", True) or getattr(result, "result_type", "complete") != "complete":
                raise WorkerError("coordinator rejected the tool call")
            value = result.structured_content
            if value is None:
                texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
                value = _loads("".join(texts))
            if not isinstance(value, dict):
                raise WorkerError("coordinator returned an invalid tool result")
            return value
        except WorkerError:
            raise
        except Exception as exc:
            # SDK/HTTP exception text can contain request details: never print it.
            raise WorkerError("coordinator call failed; local execution state was retained") from exc


@asynccontextmanager
async def connect_coordinator(config: WorkerConfig, environ: Mapping[str, str], *, transport: Any = None):
    """SDK 2.3 authenticated Streamable HTTP; no OAuth/browser side effects."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = environ.get(config.token_env)
    if not token or any(character.isspace() for character in token):
        raise WorkerError("coordinator bearer token is missing or malformed")
    async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx2.Timeout(config.request_timeout_seconds),
            follow_redirects=False, trust_env=False, transport=transport) as http:
        async with streamable_http_client(config.coordinator_url, http_client=http) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=config.request_timeout_seconds) as session:
                # The coordinator is SDK 2.3/MCP 2.0. Discover/adopt the modern
                # stateless protocol; do not silently downgrade the connection.
                await session.discover()
                yield MCPCoordinator(session, config.request_timeout_seconds)


def parse_completion(raw: bytes, output_format: str, task_id: str) -> Any:
    """Require terminal evidence in the selected executor's actual output format."""
    try:
        if output_format == "codex_jsonl":
            events = [_loads(line) for line in raw.splitlines() if line.strip()]
            if not events or any(not isinstance(event, dict) for event in events):
                raise ValueError("invalid event stream")
            if events[-1].get("type") != "turn.completed":
                raise ValueError("no terminal completed turn")
            if any(event.get("type") in {"error", "turn.failed"} for event in events):
                raise ValueError("failed turn")
            messages = [event["item"]["text"] for event in events
                        if event.get("type") == "item.completed"
                        and isinstance(event.get("item"), dict)
                        and event["item"].get("type") == "agent_message"
                        and isinstance(event["item"].get("text"), str)]
            if not messages or not messages[-1].strip():
                raise ValueError("missing final agent message")
            return messages[-1]
        value = _loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        if output_format == "claude_json":
            if (value.get("type") != "result" or value.get("subtype") != "success"
                    or value.get("is_error") is not False or not isinstance(value.get("result"), str)
                    or not value["result"].strip()):
                raise ValueError("not a successful Claude terminal result")
            return value["result"]
        if (value.get("status") != "completed" or type(value.get("exit_code")) is not int
                or value["exit_code"] != 0 or value.get("task_id") != task_id or "result" not in value):
            raise ValueError("not a completed task receipt")
        return value["result"]
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise WorkerError("executor output did not prove a terminal completed result") from exc


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop the owned process/group; uncertainty remains even after termination."""
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.returncode is None:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 1)
    except asyncio.TimeoutError:
        pass
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        pass


async def execute_command(config: WorkerConfig, payload: bytes, cwd: Path,
                          environ: Mapping[str, str], stop: asyncio.Event,
                          timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    observed = {"stdout": 0, "stderr": 0}
    overflow = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    process = None
    reason = None

    async def read_stream(name: str, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(8192):
            observed[name] += len(chunk)
            room = config.max_output_bytes - len(buffers[name])
            buffers[name].extend(chunk[:max(0, room)])
            if observed[name] > config.max_output_bytes:
                overflow.set()

    try:
        options: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        process = await asyncio.create_subprocess_exec(
            *config.argv, cwd=cwd, env=config.command_environment(environ),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, **options)
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None

        async def write_input() -> None:
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [asyncio.create_task(read_stream("stdout", process.stdout)),
                 asyncio.create_task(read_stream("stderr", process.stderr)),
                 asyncio.create_task(write_input())]
        waiter = asyncio.create_task(process.wait())
        stopped = asyncio.create_task(stop.wait())
        excessive = asyncio.create_task(overflow.wait())
        tasks.extend([waiter, stopped, excessive])
        done, _ = await asyncio.wait([waiter, stopped, excessive], timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            reason = "lease_lost"
        elif overflow.is_set():
            reason = "output_limit"
        elif waiter not in done:
            reason = "timeout"
        if reason:
            await _terminate(process)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks[:3]), 1)
        except asyncio.TimeoutError:
            reason = reason or "open_output_streams"
            await _terminate(process)
        if overflow.is_set():
            reason = reason or "output_limit"
    except asyncio.CancelledError:
        if process is not None:
            await asyncio.shield(_terminate(process))
        raise
    except (OSError, ValueError):
        reason = "launch_failed" if process is None else "execution_error"
        if process is not None:
            await _terminate(process)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return {"exit_code": process.returncode if process else None, "reason": reason,
            "stdout": bytes(buffers["stdout"]), "stderr": bytes(buffers["stderr"]),
            "stdout_bytes": observed["stdout"], "stderr_bytes": observed["stderr"],
            "duration_seconds": round(time.monotonic() - started, 3)}


class Worker:
    def __init__(self, config: WorkerConfig, coordinator: Coordinator, journal: Journal,
                 environ: Mapping[str, str] | None = None):
        self.config, self.coordinator, self.journal = config, coordinator, journal
        self.environ = os.environ if environ is None else environ

    def _receipt(self, record: dict[str, Any], outcome: str, reason: str,
                 execution: dict[str, Any] | None = None, output: Any = None) -> None:
        execution = execution or {}
        result = {"status": outcome, "exit_code": execution.get("exit_code"),
                  "reason": reason, "output": output}
        if len(_json(result)) > 60000:
            outcome = "uncertain"
            result = {"status": outcome, "exit_code": execution.get("exit_code"),
                      "reason": "completion_result_limit", "output": None}
        evidence = {"source": "worker_reported", "output_format": self.config.output_format,
                    "exit_code": execution.get("exit_code"),
                    "duration_seconds": execution.get("duration_seconds"),
                    "stdout_bytes": execution.get("stdout_bytes", 0),
                    "stderr_bytes": execution.get("stderr_bytes", 0)}
        for stream in ("stdout", "stderr"):
            value = execution.get(stream, b"")
            evidence[stream + "_captured_sha256"] = hashlib.sha256(value).hexdigest()
        record.update(phase="receipt", outcome=outcome, result=result, evidence=evidence,
                      receipt_created_at=time.time(),
                      captured_stdout=execution.get("stdout", b"").decode("utf-8", "replace"),
                      captured_stderr=execution.get("stderr", b"").decode("utf-8", "replace"))
        self.journal.save(record)  # Must happen before any completion RPC.

    @staticmethod
    def _matches(task: dict[str, Any], record: dict[str, Any]) -> bool:
        if (task.get("task_id") != record["task_id"] or type(task.get("fence")) is not int
                or task["fence"] != record["fence"] or task.get("status") != record["outcome"]):
            return False
        try:
            # Python equality conflates True/1 and 1/1.0. The coordinator's
            # immutable completion includes both result AND evidence.
            return _json({key: task.get(key) for key in ("result", "evidence")}) == _json({
                key: record[key] for key in ("result", "evidence")})
        except (ValueError, TypeError):
            return False

    async def _deliver(self, record: dict[str, Any]) -> dict[str, Any]:
        args = {key: record[key] for key in ("task_id", "lease_token", "fence", "result", "outcome", "evidence")}
        try:
            task = await self.coordinator.call("aster_complete_task", args)
        except Exception:
            try:
                task = await self.coordinator.call("aster_task_status", {"task_id": record["task_id"]})
            except Exception:
                return {"state": "receipt_pending", "task_id": record["task_id"]}
        if self._matches(task, record):
            path = self.journal.archive(record, "acknowledged")
            return {"state": record["outcome"], "task_id": record["task_id"], "receipt": str(path)}
        # A lease fenced by cancellation, replacement or expiry cannot authorize completion.
        if (task.get("task_id") == record["task_id"] and
                (task.get("fence") != record["fence"] or task.get("status") in {
                    "completed", "failed", "uncertain", "blocked", "cancelled", "expired"})):
            record["observed_remote_status"] = task.get("status")
            path = self.journal.archive(record, "reconciliation_required")
            return {"state": "reconciliation_required", "task_id": record["task_id"], "receipt": str(path)}
        # An ack/running response is not proof that the completion was persisted.
        return {"state": "receipt_pending", "task_id": record["task_id"]}

    async def _execute(self, record: dict[str, Any], task: dict[str, Any]) -> None:
        config = self.config
        public_task = {key: task.get(key) for key in (
            "task_id", "instruction", "expected_result", "inputs", "authorized_scope", "deadline")}
        try:
            payload = _json({"version": 1, "task": public_task}) + b"\n"
        except (ValueError, TypeError):
            self._receipt(record, "blocked", "invalid_task_input")
            return
        if len(payload) > config.max_input_bytes:
            self._receipt(record, "blocked", "task_input_limit")
            return
        deadline = task.get("deadline")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            self._receipt(record, "blocked", "invalid_task_deadline")
            return
        runtime = min(config.timeout_seconds, deadline - time.time())
        if runtime <= 0 or record["lease_expires_at"] <= time.time() + config.request_timeout_seconds:
            self._receipt(record, "blocked", "insufficient_lease_or_deadline")
            return
        cwd = config.workspace
        if config.workspace_mode == "isolated":
            suffix = hashlib.sha256(f"{record['task_id']}:{record['fence']}".encode()).hexdigest()
            cwd = cwd / suffix
            try:
                cwd.mkdir(mode=0o700, exist_ok=False)
            except FileExistsError:
                self._receipt(record, "uncertain", "attempt_workspace_already_exists")
                return
        record.update(phase="running", execution_started_at=time.time(), workspace=str(cwd))
        self.journal.save(record)  # Crash after this point must never rerun this attempt.
        # Directory creation/fsync may block. A lease checked before them is not
        # authority to launch afterwards, and the old runtime could overrun the
        # task deadline by however long persistence took.
        now = time.time()
        runtime = min(config.timeout_seconds, deadline - now)
        if runtime <= 0 or record["lease_expires_at"] <= now + config.request_timeout_seconds:
            self._receipt(record, "blocked", "lease_or_deadline_elapsed_before_launch")
            return
        stop = asyncio.Event()
        finished = asyncio.Event()

        async def renew() -> None:
            while not finished.is_set():
                try:
                    await asyncio.wait_for(finished.wait(), config.renew_interval_seconds)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    renewed = await self.coordinator.call("aster_renew_lease", {
                        "task_id": record["task_id"], "lease_token": record["lease_token"],
                        "fence": record["fence"], "lease_seconds": config.lease_seconds})
                    renewed_task = renewed.get("task")
                    expires = renewed.get("lease_expires_at")
                    if (type(renewed.get("fence")) is not int
                            or renewed["fence"] != record["fence"]
                            or renewed.get("lease_token") != record["lease_token"]
                            or not isinstance(renewed_task, dict)
                            or renewed_task.get("task_id") != record["task_id"]
                            or renewed_task.get("status") != "running"
                            or type(renewed_task.get("fence")) is not int
                            or renewed_task["fence"] != record["fence"]
                            or isinstance(expires, bool) or not isinstance(expires, (int, float))
                            or not math.isfinite(expires)
                            or expires <= time.time() + config.request_timeout_seconds):
                        raise WorkerError("lease renewal did not confirm ownership")
                except Exception:
                    stop.set()
                    return

        heartbeat = asyncio.create_task(renew())
        try:
            execution = await execute_command(config, payload, cwd, self.environ, stop, runtime)
            if stop.is_set():
                execution["reason"] = "lease_lost"
            reason = execution["reason"]
            output = None
            if reason:
                outcome = "blocked" if reason == "launch_failed" else "uncertain"
            elif execution["exit_code"] != 0:
                outcome, reason = "failed", "nonzero_exit"
            else:
                try:
                    output = parse_completion(execution["stdout"], config.output_format, record["task_id"])
                    outcome, reason = "completed", "terminal_result_observed"
                except WorkerError:
                    outcome, reason = "uncertain", "invalid_terminal_result"
            self._receipt(record, outcome, reason, execution, output)
        except asyncio.CancelledError:
            self._receipt(record, "uncertain", "worker_interrupted")
            raise
        except Exception:
            self._receipt(record, "uncertain", "worker_execution_error")
        finally:
            finished.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run_once(self) -> dict[str, Any]:
        record = self.journal.load()
        if record is None:
            self.config.preflight(self.environ)
            record = {"version": 1, "phase": "claiming", "request_id": str(uuid.uuid4()),
                      "coordinator_url": self.config.coordinator_url, "created_at": time.time()}
            self.journal.save(record)
        if record.get("coordinator_url") != self.config.coordinator_url:
            raise WorkerError("journal belongs to a different coordinator; use its original configuration")
        if record["phase"] == "running":
            self._receipt(record, "uncertain", "interrupted_attempt_not_reexecuted")
        if record["phase"] == "receipt":
            return await self._deliver(record)
        if record["phase"] != "claiming":
            raise WorkerError("unknown journal phase; operator reconciliation required")
        self.config.preflight(self.environ)
        claim = await self.coordinator.call("aster_claim_task", {
            "request_id": record["request_id"], "lease_seconds": self.config.lease_seconds})
        if "task" not in claim:
            raise WorkerError("coordinator omitted the task from its claim response")
        task = claim.get("task")
        if task is None:
            self.journal.clear()
            return {"state": "idle"}
        if (not isinstance(task, dict) or not isinstance(task.get("task_id"), str)
                or not task["task_id"] or task.get("status") != "running"
                or not isinstance(claim.get("lease_token"), str) or not claim["lease_token"]
                or type(claim.get("fence")) is not int or claim["fence"] < 1
                or type(task.get("fence")) is not int
                or task.get("fence") != claim["fence"]):
            raise WorkerError("coordinator returned an invalid claim; no execution started")
        expires = claim.get("lease_expires_at")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise WorkerError("coordinator returned an invalid lease expiry")
        record.update(task_id=task["task_id"], fence=claim["fence"], lease_token=claim["lease_token"],
                      lease_expires_at=expires)
        await self._execute(record, task)
        return await self._deliver(record)


async def run(config: WorkerConfig, *, watch: bool = False) -> int:
    with Journal(config.state_dir) as journal:
        async with connect_coordinator(config, os.environ) as coordinator:
            worker = Worker(config, coordinator, journal)
            while True:
                summary = await worker.run_once()
                print(json.dumps(summary, sort_keys=True), flush=True)
                state = summary["state"]
                if state not in {"completed", "idle"}:
                    return 2
                if not watch:
                    return 0
                await asyncio.sleep(config.poll_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="operator-owned worker JSON configuration")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="process at most one task (default)")
    modes.add_argument("--watch", action="store_true", help="explicitly poll for tasks until stopped or attention is needed")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(WorkerConfig.load(args.config), watch=args.watch))
    except KeyboardInterrupt:
        print("worker interrupted; inspect retained execution state before retrying", file=sys.stderr)
        return 130
    except WorkerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("worker stopped; execution state retained for recovery (no automatic redispatch)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
