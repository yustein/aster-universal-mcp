"""Recovery/configuration safety only; never touches systemd or real credentials."""
from __future__ import annotations

import ast
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import io
from contextlib import redirect_stdout
import tempfile
import unittest
from unittest import mock

from coordination.store import Store
from client_registry import ClientRegistry
from oauth_codes import AuthorizationCodeStore
from token_store import TokenStore
from refresh_store import RefreshStore
from approval_store import ApprovalStore
from approval_sessions import ApprovalSessions

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("coordinator_operator", ROOT / "ops/coordinator/operator.py")
op = importlib.util.module_from_spec(spec)
spec.loader.exec_module(op)


def stopped():
    return {"unit": op.UNIT, "active_state": "inactive", "main_pid": 0}


def installation(tmp_path):
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = tmp_path / "service.env"
    op.prepare_config(config, "https://coord.example.test", state)
    store = Store(state / "tasks.sqlite")
    store.register_agent(agent_id="sender", client_id="sender-client", display_name="sender", provider="fixture")
    store.register_agent(agent_id="worker", client_id="worker-client", display_name="worker", provider="fixture")
    store.grant(sender="sender", receiver="worker")
    task = store.delegate_task("sender-client", to_agent="worker", request_id="fixture-delegate",
                              instruction="fixture", expected_result="fixture")
    claim = store.claim_task("worker-client", request_id="fixture-claim", task_id=task["task_id"])
    finished = store.complete_task("worker-client", task_id=task["task_id"], lease_token=claim["lease_token"],
                                   fence=claim["fence"], result={"secret": "TASK_SECRET_MUST_NOT_PRINT"})
    store.verify_result("sender-client", task_id=task["task_id"], expected_revision=finished["revision"],
                        accepted=True, evidence={"checks": ["fixture check"], "reference": "fixture-evidence"})
    # Parse the actual schema literal without importing the optional MCP SDK.
    tree = ast.parse((ROOT / "coordination/native.py").read_text())
    schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "NATIVE_SCHEMA" for t in n.targets))
    with sqlite3.connect(state / "tasks.sqlite") as db:
        db.executescript(schema)
        db.execute("""INSERT INTO coord_native_subscriptions
            (id,identity_key,agent_id,client_id,expected_session_id,url,secret,created_at,expires_at,active,version)
            VALUES ('fixture','fixture','worker','worker-client','fixture-session',
                    'https://callback.example.test/private','NATIVE_SECRET_MUST_NOT_PRINT',1,2,1,1)""")
    oauth = state / "oauth.sqlite"
    op.new_file(oauth, b"")
    # Use the actual fresh-service stores, including lazy absence of pairing.
    registry = ClientRegistry(str(oauth))
    registry.register_external("sender-client", "fixture sender", ["https://client.example.test/callback"])
    registry.register_external("worker-client", "fixture worker", ["https://client.example.test/callback"])
    for constructor in (AuthorizationCodeStore, TokenStore, RefreshStore, ApprovalStore, ApprovalSessions):
        constructor(str(oauth))
    with sqlite3.connect(oauth) as db:
        db.execute("CREATE TABLE client_metadata (client_id TEXT PRIMARY KEY, json TEXT NOT NULL)")
        db.execute("INSERT INTO access_tokens VALUES (?,?,?,?,?,0)",
                   ("OAUTH_SECRET_MUST_NOT_PRINT", "worker-client", "https://coord.example.test/mcp", "system:admin", 2000000000))
    op.new_file(state / "approved-clients.json", b'["sender-client","worker-client"]\n')
    return SimpleNamespace(root=tmp_path, state=state, config=config, task_id=task["task_id"])


def take_backup(env):
    output = env.root / "backup"
    op.snapshot(env.config, output, state_root=env.state, quiescence=stopped)
    return output


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aster-deployment-test-")
        self.addCleanup(self.temp.cleanup)
        self.env = installation(Path(self.temp.name))

    def test_committed_wal_and_actual_isolated_restore(self):
        env = self.env
        with sqlite3.connect(env.state / "tasks.sqlite") as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE coord_wal_fixture (value TEXT)")
            writer.execute("INSERT INTO coord_wal_fixture VALUES ('COMMITTED_WAL_SECRET')")
            writer.commit()
            self.assertGreater((env.state / "tasks.sqlite-wal").stat().st_size, 0)
            before = op.digest(env.state / "tasks.sqlite-wal")
            backup = take_backup(env)
            self.assertEqual(op.digest(env.state / "tasks.sqlite-wal"), before)
        with op.readonly_db(backup / "tasks.sqlite") as restored:
            self.assertEqual(restored.execute("SELECT value FROM coord_wal_fixture").fetchone(), ("COMMITTED_WAL_SECRET",))
            task = json.loads(restored.execute("SELECT document FROM coord_tasks WHERE task_id=?", (env.task_id,)).fetchone()[0])
            self.assertEqual(task["acceptance"], "accepted")
        output = io.StringIO()
        with redirect_stdout(output):
            status = op.main(["restore-verify", "--backup", str(backup), "--work-parent", str(env.root)])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["application"]["accepted_tasks"], 1)
        self.assertTrue(result["restored_offline"] and result["temporary_state_removed"])
        self.assertFalse(result["service_started"] or result["callbacks_dispatched"])
        self.assertFalse(list(env.root.glob("coordinator-restore-*")))
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("callback.example", output.getvalue())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in backup.iterdir()))

    def test_fresh_oauth_without_lazy_pairing_table(self):
        env = self.env
        with op.readonly_db(env.state / "oauth.sqlite") as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='pairing'").fetchone())
        backup = take_backup(env)
        self.assertEqual(op.verify_snapshot(backup)["application"]["oauth_clients"], 2)

    def test_backup_refuses_active_service_and_shared_application_lock(self):
        env = self.env
        response = SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=active\nMainPID=100\n")
        with mock.patch.object(op.subprocess, "run", return_value=response) as runner:
            with self.assertRaisesRegex(op.OperatorError, "stop the Coordinator"):
                op.snapshot(env.config, env.root / "backup", state_root=env.state, quiescence=op.service_stopped)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0][1], "show")
        self.assertFalse((env.root / "backup").exists())
        with (env.state / "operator.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaisesRegex(op.OperatorError, "holds the state lock"):
                take_backup(env)
        self.assertFalse(list(env.root.glob(".coordinator-backup-*")))

    def test_install_requires_known_inactive_or_absent_service(self):
        cases = [
            (0, "LoadState=not-found\nActiveState=inactive\nMainPID=0\n", True),
            (0, "LoadState=loaded\nActiveState=inactive\nMainPID=0\n", True),
            (0, "LoadState=loaded\nActiveState=activating\nMainPID=0\n", False),
            (1, "", False),
            (0, "LoadState=loaded\nActiveState=inactive\nMainPID=123\n", False),
        ]
        for code, output, allowed in cases:
            with self.subTest(code=code, allowed=allowed, output=output):
                with mock.patch.object(op.subprocess, "run", return_value=SimpleNamespace(returncode=code, stdout=output)):
                    if allowed:
                        self.assertEqual(op.service_stopped(allow_missing=True)["main_pid"], 0)
                    else:
                        with self.assertRaises(op.OperatorError):
                            op.service_stopped(allow_missing=True)

    def test_partial_backup_cleanup_and_no_overwrite(self):
        env = self.env
        original = op.db_snapshot
        count = 0
        def fail_second(src, dst):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected fixture failure")
            return original(src, dst)
        with mock.patch.object(op, "db_snapshot", side_effect=fail_second):
            with self.assertRaises(OSError):
                take_backup(env)
        self.assertFalse((env.root / "backup").exists())
        self.assertFalse(list(env.root.glob(".coordinator-backup-*")))
        backup = take_backup(env)
        before = {p.name: op.digest(p) for p in backup.iterdir()}
        with self.assertRaisesRegex(op.OperatorError, "new destination"):
            take_backup(env)
        self.assertEqual(before, {p.name: op.digest(p) for p in backup.iterdir()})

    def test_restore_rejects_tampering_before_creating_copy(self):
        for damage in ("content", "missing", "symlink", "extra", "manifest_names"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory(prefix="aster-tamper-") as tmp:
                env = installation(Path(tmp))
                backup = take_backup(env)
                target = backup / "oauth.sqlite"
                if damage == "content":
                    with target.open("ab") as stream:
                        stream.write(b"tamper")
                elif damage == "missing":
                    target.unlink()
                elif damage == "symlink":
                    target.unlink()
                    target.symlink_to(env.state / "oauth.sqlite")
                elif damage == "extra":
                    op.new_file(backup / "unexpected", b"extra")
                else:
                    manifest = json.loads((backup / "manifest.json").read_text())
                    manifest["files"]["../outside"] = manifest["files"].pop("oauth.sqlite")
                    (backup / "manifest.json").write_text(json.dumps(manifest))
                with self.assertRaises(op.OperatorError):
                    op.restore_verify(backup, env.root)
                self.assertFalse(list(env.root.glob("coordinator-restore-*")))

    def test_symlink_sources_and_restore_live_tree_rejected(self):
        env = self.env
        backup = take_backup(env)
        with self.assertRaisesRegex(op.OperatorError, "outside live"):
            op.restore_verify(backup, env.state)
        target = env.state / "approved-clients.json"
        before = target.read_bytes()
        outside = env.root / "outside"
        op.new_file(outside, before)
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(op.OperatorError, "symlink"):
            op.snapshot(env.config, env.root / "new-backup", state_root=env.state, quiescence=stopped)
        self.assertEqual(outside.read_bytes(), before)
        link = env.root / "linked"
        link.symlink_to(env.root, target_is_directory=True)
        with self.assertRaisesRegex(op.OperatorError, "symlink"):
            op.restore_verify(backup, link)

    def test_invalid_configuration_rejected_without_mutation(self):
        env = self.env
        original = env.config.read_bytes()
        for old, new in (
            ("https://coord.example.test", "http://coord.example.test"),
            ("https://coord.example.test", "https://coord.example.test/"),
            ("https://coord.example.test", "https://127.0.0.1"),
            ("https://coord.example.test", "https://coord.example.test:443"),
            ("ASTER_COORDINATION_PORT=18769", "ASTER_COORDINATION_PORT=80"),
            ("ASTER_COORDINATION_NATIVE_EVENTS=1", "ASTER_COORDINATION_NATIVE_EVENTS=true"),
            ("/tasks.sqlite", "/oauth.sqlite"),
        ):
            with self.subTest(old=old, new=new):
                raw = original.replace(old.encode(), new.encode())
                env.config.write_bytes(raw)
                with self.assertRaises(op.OperatorError):
                    op.load_config(env.config, env.state)
                self.assertEqual(env.config.read_bytes(), raw)

    def test_config_preparation_idempotence_and_origin_preservation(self):
        env = self.env
        raw = b"# Preserve existing operator choices\n" + env.config.read_bytes().replace(b"18769", b"18770")
        env.config.write_bytes(raw)
        before = env.config.stat().st_mtime_ns
        self.assertEqual(op.prepare_config(env.config, "https://coord.example.test", env.state), {"created": False, "preserved": True})
        self.assertEqual(env.config.read_bytes(), raw)
        self.assertEqual(env.config.stat().st_mtime_ns, before)
        with self.assertRaisesRegex(op.OperatorError, "origin differs"):
            op.prepare_config(env.config, "https://other.example.test", env.state)
        self.assertEqual(env.config.read_bytes(), raw)

    def test_cli_failure_does_not_echo_malformed_secret(self):
        env = self.env
        backup = take_backup(env)
        (backup / "manifest.json").write_text("SECRET_CREDENTIAL_BYTES{")
        output = io.StringIO()
        with redirect_stdout(output):
            status = op.main(["restore-verify", "--backup", str(backup), "--work-parent", str(env.root)])
        self.assertEqual(status, 2)
        self.assertNotIn("SECRET", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
