"""Frozen-candidate and gateway-lease boundaries, without Windows executors.

The portable cases exercise the exact configuration/lease module on either OS.
The Windows-only case additionally verifies the native process identity fields.
"""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


MODULE_PATH = (Path(__file__).resolve().parents[1] / "platforms" / "windows"
               / "runtime_manifest.py")
SPEC = importlib.util.spec_from_file_location("aster_test_runtime_manifest", MODULE_PATH)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@pytest.fixture(autouse=True)
def isolated_launch_environment(monkeypatch):
    # Register each key with monkeypatch even if it was originally absent:
    # load_configuration itself may add it before the test exits.
    for name in runtime.ENV_KEYS.values():
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "source"
    gateway = root / "platforms" / "windows" / "oauth_gateway.py"
    gateway.parent.mkdir(parents=True)
    gateway.write_text("# Frozen gateway fixture\n", encoding="utf-8")
    pairing = root / "pairing.py"
    pairing.write_text("# Frozen pairing fixture\n", encoding="utf-8")
    state = tmp_path / "protected-state"
    state.mkdir()
    workspace = state / "workspace"
    workspace.mkdir()
    manifest = state / "candidate-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "files": [
            {"path": p.relative_to(root).as_posix(),
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in (gateway, pairing)
        ],
    }, indent=2) + "\n", encoding="utf-8")
    allowlist = state / "admin-clients.json"
    allowlist.write_text("[]\n", encoding="utf-8-sig")
    config = {
        "schema_version": 1,
        "source_root": str(root),
        "python_executable": str(Path(sys.executable).resolve()),
        "workspace_root": str(workspace),
        "oauth_db": str(state / "windows-oauth.sqlite"),
        "admin_clients": str(allowlist),
        "source_manifest": str(manifest),
        "runtime_manifest": str(state / "gateway-runtime.json"),
        "public_origin": "https://mcp.example.invalid",
        "port": 18767,
        "candidate_id": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    path = state / "gateway-config.json"

    def write_config():
        path.write_text(json.dumps(config) + "\n", encoding="utf-8-sig")

    def load():
        return runtime.load_configuration(path, source_root=root)

    write_config()
    return SimpleNamespace(root=root, state=state, workspace=workspace, path=path, config=config,
                           manifest=manifest, gateway=gateway, pairing=pairing,
                           write_config=write_config, load=load)


def test_valid_bom_configuration_exports_the_verified_identity(deployment):
    loaded = deployment.load()
    assert loaded["candidate_id"] == hashlib.sha256(
        deployment.manifest.read_bytes()).hexdigest()
    assert loaded["source_root"] == str(deployment.root.resolve())
    for key, name in runtime.ENV_KEYS.items():
        assert os.environ[name] == str(loaded[key])
    assert not Path(loaded["runtime_manifest"]).exists()
    assert not Path(loaded["oauth_db"]).exists()


def test_default_workspace_output_preserves_frozen_source(deployment, monkeypatch):
    loaded = deployment.load()
    spec = importlib.util.spec_from_file_location(
        "workspace_executor", MODULE_PATH.with_name("executor.py"))
    executor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(executor)
    # The startup entrypoint changes into this validated directory before
    # accepting tools; the public command keeps its existing cwd='.' default.
    monkeypatch.chdir(loaded["workspace_root"])
    result = executor.run_command([
        sys.executable, "-c",
        "from pathlib import Path; Path('claude_test.txt').write_text('workspace proof', encoding='utf-8')",
    ], timeout=10)
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert (deployment.workspace / "claude_test.txt").read_text() == "workspace proof"
    assert not (deployment.root / "claude_test.txt").exists()
    assert deployment.load()["candidate_id"] == loaded["candidate_id"]


@pytest.mark.parametrize("location", ["source", "outside", "missing"])
def test_workspace_must_be_the_real_external_state_workspace(deployment, location):
    destination = {
        "source": deployment.root,
        "outside": deployment.state.parent,
        "missing": deployment.state / "missing",
    }[location]
    deployment.config["workspace_root"] = str(destination)
    deployment.write_config()
    with pytest.raises(ValueError, match="workspace_root"):
        deployment.load()


def test_unexpected_source_output_is_still_rejected(deployment):
    (deployment.root / "unexpected.txt").write_text("outside the workspace", encoding="utf-8")
    with pytest.raises(ValueError, match="source file inventory"):
        deployment.load()


@pytest.mark.parametrize("filename", ["gateway", "pairing"])
def test_changed_source_is_rejected_before_exporting_environment(deployment, filename):
    getattr(deployment, filename).write_text("# Changed after freezing\n", encoding="utf-8")
    before = dict(os.environ)
    with pytest.raises(ValueError, match="candidate source changed"):
        deployment.load()
    assert dict(os.environ) == before


def test_candidate_manifest_bytes_must_match_configured_hash(deployment):
    # Whitespace preserves the JSON meaning but changes the frozen artifact.
    with deployment.manifest.open("ab") as handle:
        handle.write(b"\n")
    with pytest.raises(ValueError, match="configured candidate"):
        deployment.load()


@pytest.mark.parametrize("key", ["source_root", "python_executable"])
def test_configuration_cannot_identify_a_different_process_or_tree(deployment, key):
    deployment.config[key] = str(deployment.state / "different-location")
    deployment.write_config()
    with pytest.raises(ValueError, match="different source|interpreter"):
        deployment.load()


@pytest.mark.parametrize("key", ["oauth_db", "admin_clients", "source_manifest",
                                 "runtime_manifest"])
def test_state_paths_cannot_escape_configuration_directory(deployment, key):
    deployment.config[key] = str(deployment.root / "outside-state")
    deployment.write_config()
    with pytest.raises(ValueError, match="protected configuration directory"):
        deployment.load()


@pytest.mark.parametrize("alias", ["oauth_db", "admin_clients", "source_manifest",
                                   "config_file", "lease_lock"])
def test_runtime_identity_cannot_overwrite_other_protected_state(deployment, alias):
    if alias == "config_file":
        deployment.config["runtime_manifest"] = str(deployment.path)
    elif alias == "lease_lock":
        # An identity named *.json also owns the corresponding *.lock file.
        deployment.config["oauth_db"] = str(
            Path(deployment.config["runtime_manifest"]).with_suffix(".lock"))
    else:
        deployment.config["runtime_manifest"] = deployment.config[alias]
    deployment.write_config()
    before = {p: p.read_bytes() for p in deployment.state.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        deployment.load()
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("key,bad_value", [
    ("public_origin", "https://other.example.invalid"),
    ("oauth_db", "other-oauth.sqlite"),
    ("admin_clients", "other-clients.json"),
    ("runtime_manifest", "other-runtime.json"),
    ("workspace_root", "other-workspace"),
    ("port", "18768"),
])
def test_conflicting_environment_is_rejected_without_partial_export(
        deployment, monkeypatch, key, bad_value):
    if key in ("oauth_db", "admin_clients", "runtime_manifest", "workspace_root"):
        bad_value = str(deployment.state / bad_value)
    name = runtime.ENV_KEYS[key]
    monkeypatch.setenv(name, bad_value)
    before = dict(os.environ)
    with pytest.raises(ValueError, match=name):
        deployment.load()
    assert dict(os.environ) == before


def test_matching_environment_does_not_create_a_second_configuration(
        deployment, monkeypatch):
    for key, name in runtime.ENV_KEYS.items():
        monkeypatch.setenv(name, str(deployment.config[key]))
    assert deployment.load()["candidate_id"] == deployment.config["candidate_id"]


@pytest.mark.parametrize("failure", [None, RuntimeError("startup failed")])
def test_lease_cleans_identity_and_releases_lock_after_exit(deployment, failure):
    config = deployment.load()
    path = Path(config["runtime_manifest"])
    before_allowlist = Path(config["admin_clients"]).read_bytes()

    def enter():
        with runtime.runtime_lease(config) as identity:
            assert identity["pid"] == os.getpid()
            assert identity["candidate_id"] == config["candidate_id"]
            assert identity["workspace_root"] == config["workspace_root"]
            assert json.loads(path.read_text(encoding="utf-8")) == identity
            assert datetime.fromisoformat(identity["started_at"].replace("Z", "+00:00")).tzinfo
            if failure is not None:
                raise failure

    if failure is None:
        enter()
    else:
        with pytest.raises(RuntimeError, match="startup failed"):
            enter()
    assert not path.exists()
    assert Path(config["admin_clients"]).read_bytes() == before_allowlist
    with runtime.runtime_lease(config):
        assert path.exists()
    assert not path.exists()


def test_second_lease_cannot_replace_or_remove_live_identity(deployment):
    config = deployment.load()
    path = Path(config["runtime_manifest"])
    with runtime.runtime_lease(config):
        original = path.read_bytes()
        with pytest.raises(OSError):
            with runtime.runtime_lease(config):
                pytest.fail("second live lease unexpectedly acquired")
        assert path.read_bytes() == original
    assert not path.exists()


def test_lease_excludes_another_process_and_allows_it_after_release(deployment):
    config = deployment.load()
    child = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('runtime_under_test', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    with module.runtime_lease(json.loads(sys.argv[2])):
        print('ACQUIRED', flush=True)
except OSError:
    print('BUSY', flush=True)
    raise SystemExit(3)
"""

    def attempt():
        return subprocess.run(
            [sys.executable, "-c", child, str(MODULE_PATH), json.dumps(config)],
            capture_output=True, text=True, timeout=15, check=False)

    with runtime.runtime_lease(config):
        result = attempt()
        assert result.returncode == 3, result.stderr
        assert result.stdout.strip() == "BUSY"
    result = attempt()
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ACQUIRED"
    assert not Path(config["runtime_manifest"]).exists()


@pytest.mark.parametrize("field", ["pid", "candidate_id"])
def test_cleanup_preserves_an_identity_it_no_longer_owns(deployment, field):
    config = deployment.load()
    path = Path(config["runtime_manifest"])
    with runtime.runtime_lease(config) as identity:
        foreign = dict(identity)
        foreign[field] = os.getpid() + 1000 if field == "pid" else "different-candidate"
        path.write_text(json.dumps(foreign), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == foreign


@pytest.mark.parametrize("stage", ["_process_identity", "_write_atomic"])
def test_startup_failure_releases_the_exclusive_lease(deployment, monkeypatch, stage):
    config = deployment.load()

    def fail(*args, **kwargs):
        raise OSError("identity startup failure")

    with monkeypatch.context() as patch:
        patch.setattr(runtime, stage, fail)
        with pytest.raises(OSError, match="identity startup failure"):
            with runtime.runtime_lease(config):
                pytest.fail("failed startup yielded a live identity")
    assert not Path(config["runtime_manifest"]).exists()
    with runtime.runtime_lease(config):
        assert Path(config["runtime_manifest"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="native Win32 identity requires Windows")
def test_windows_identity_records_real_process_image_and_creation_time():
    before = datetime.now(timezone.utc)
    identity = runtime._process_identity()
    assert Path(identity["process_executable"]).is_absolute()
    assert Path(identity["process_executable"]).is_file()
    created = datetime.fromisoformat(identity["process_started_at"].replace("Z", "+00:00"))
    assert created.tzinfo is not None
    assert created <= before
    assert isinstance(identity["elevated"], bool)
