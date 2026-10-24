"""Native tests of the installed recovery path without modifying real ProgramData."""
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="protected operator requires Windows ACLs")
ROOT = Path(__file__).resolve().parents[1]


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def protected_operator(tmp_path):
    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("protected operator tests require an elevated Windows test runner")
    source = tmp_path / "source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    for name in ("windows-common.ps1", "status-windows.ps1", "manage-client-windows.ps1"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    (source / "platforms" / "windows").mkdir(parents=True)
    (source / "platforms" / "windows" / "oauth_gateway.py").write_text("# fixture\n")
    (source / "admin_client_allowlist.py").write_text("raise RuntimeError('repo Python must not run')\n")
    python = source / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"unused fixture executable")
    program_data = tmp_path / "program-data"
    state = program_data / "AsterUniversalMCP"
    state.mkdir(parents=True)
    workspace = state / "workspace"
    workspace.mkdir()
    operator = state / "operator-windows.ps1"
    shutil.copyfile(ROOT / "scripts" / "operator-windows.ps1", operator)
    manifest = state / "candidate-manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "files": [
        {"path": p.relative_to(source).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(source.rglob("*")) if p.is_file() and ".venv" not in p.parts
    ]}) + "\n", encoding="utf-8")
    clients = ["test-client-a", "test-client-b"]
    allowlist = state / "admin-clients.json"
    allowlist.write_text(json.dumps(clients), encoding="utf-8")
    config = {
        "schema_version": 1, "source_root": str(source), "python_executable": str(python),
        "workspace_root": str(workspace), "operator_script": str(operator),
        "operator_sha256": hashlib.sha256(operator.read_bytes()).hexdigest(),
        "source_manifest": str(manifest), "candidate_id": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "runtime_manifest": str(state / "gateway-runtime.json"),
        "admin_clients": str(allowlist), "oauth_db": str(state / "windows-oauth.sqlite"),
        "public_origin": "https://mcp.example.invalid", "port": 18767,
    }
    config_path = state / "gateway-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    env = dict(os.environ, ProgramData=str(program_data))

    def run(script, *args):
        return subprocess.run([
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), *args,
        ], env=env, capture_output=True, text=True, timeout=30, check=False)

    bootstrap = tmp_path / "protect.ps1"
    controls = ", ".join(ps_quote(p) for p in (config_path, manifest, operator, allowlist))
    bootstrap.write_text(
        f". {ps_quote(scripts / 'windows-common.ps1')}\n"
        f"$controls = @({controls})\n"
        "$caller = [Security.Principal.WindowsIdentity]::GetCurrent().User\n"
        "foreach ($control in $controls) {\n"
        "  $acl = Get-Acl -LiteralPath $control\n  $acl.SetOwner($caller)\n"
        "  Set-Acl -LiteralPath $control -AclObject $acl\n"
        "  if ((Get-Acl -LiteralPath $control).GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $caller.Value) { throw 'Fixture control owner was not established' }\n"
        "}\n"
        f"Protect-AsterState {ps_quote(state)}\n"
        "foreach ($control in $controls) {\n  Protect-AsterControlFileOwner $control\n"
        "  if ((Get-Acl -LiteralPath $control).GetOwner([Security.Principal.SecurityIdentifier]).Value -ne 'S-1-5-32-544') { throw 'Setup control owner normalization failed' }\n"
        "}\n'CONTROL_OWNERS_NORMALIZED'\n"
        "$null = Get-AsterWindowsContext\n'VERIFIED_CONTEXT'\n", encoding="utf-8")
    result = run(bootstrap)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERIFIED_CONTEXT" in result.stdout
    assert "CONTROL_OWNERS_NORMALIZED" in result.stdout
    return {"source": source, "scripts": scripts, "state": state, "operator": operator,
            "config": config, "config_path": config_path, "allowlist": allowlist, "run": run}


def test_protected_revoke_and_disable_work_after_source_drift(protected_operator):
    fixture = protected_operator
    (fixture["source"] / "claude_test.txt").write_text("deliberate source drift", encoding="utf-8")
    blocked = fixture["run"](fixture["scripts"] / "status-windows.ps1")
    assert blocked.returncode != 0
    assert "Source file inventory differs" in blocked.stdout
    # Even replacing a repository helper cannot affect the installed entrypoint.
    (fixture["scripts"] / "windows-common.ps1").write_text("throw 'repository helper changed'\n")
    revoked = fixture["run"](fixture["operator"], "-Action", "revoke", "-ClientId", "test-client-a")
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    result = json.loads(revoked.stdout)
    assert result["protected_state_verified"] is True and result["removed"] == 1
    assert json.loads(fixture["allowlist"].read_text()) == ["test-client-b"]
    disabled = fixture["run"](fixture["operator"], "-Action", "disable")
    assert disabled.returncode == 0, disabled.stdout + disabled.stderr
    assert json.loads(disabled.stdout)["status"] == "disabled"
    assert json.loads(fixture["allowlist"].read_text()) == []
    assert fixture["config_path"].is_file()
    assert (fixture["source"] / "claude_test.txt").exists()


def test_protected_operator_rejects_state_path_redirection(protected_operator):
    fixture = protected_operator
    outsider = fixture["source"] / "unrelated.json"
    outsider.write_text('["untouched"]', encoding="utf-8")
    fixture["config"]["admin_clients"] = str(outsider)
    fixture["config_path"].write_text(json.dumps(fixture["config"]), encoding="utf-8")
    result = fixture["run"](fixture["operator"], "-Action", "disable")
    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "blocked"
    assert json.loads(outsider.read_text()) == ["untouched"]
    assert json.loads(fixture["allowlist"].read_text()) == ["test-client-a", "test-client-b"]


def test_allowlist_owner_normalization_after_elevated_caller_replacement(protected_operator, tmp_path):
    fixture = protected_operator
    normalize = tmp_path / "normalize-owner.ps1"
    normalize.write_text(
        f". {ps_quote(fixture['scripts'] / 'windows-common.ps1')}\n"
        f"$path = {ps_quote(fixture['allowlist'])}\n"
        "$caller = [Security.Principal.WindowsIdentity]::GetCurrent().User\n"
        "$acl = Get-Acl -LiteralPath $path\n$acl.SetOwner($caller)\n"
        "Set-Acl -LiteralPath $path -AclObject $acl\n"
        "if ((Get-Acl -LiteralPath $path).GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $caller.Value) { throw 'Fixture owner was not established' }\n"
        "Protect-AsterAllowlistOwner\n"
        "[pscustomobject]@{owner = (Get-Acl -LiteralPath $path).GetOwner([Security.Principal.SecurityIdentifier]).Value} | ConvertTo-Json\n",
        encoding="utf-8")
    normalized = fixture["run"](normalize)
    assert normalized.returncode == 0, normalized.stdout + normalized.stderr
    assert json.loads(normalized.stdout)["owner"] == "S-1-5-32-544"
    revoked = fixture["run"](fixture["operator"], "-Action", "revoke", "-ClientId", "test-client-a")
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    assert json.loads(fixture["allowlist"].read_text()) == ["test-client-b"]


def test_protected_allowlist_json_array_shapes(protected_operator, tmp_path):
    fixture = protected_operator
    cases = [[], ["one"], ["one", "two"], [None], [[]], [1], None, {"client": "one"}]
    probe = tmp_path / "allowlist-arrays.ps1"
    literals = ", ".join(ps_quote(json.dumps(value)) for value in cases)
    probe.write_text(
        f"$path = {ps_quote(fixture['allowlist'])}\n"
        f"$operator = {ps_quote(fixture['operator'])}\n"
        f"$cases = @({literals})\n$results = @()\n"
        "foreach ($raw in $cases) {\n"
        "  [IO.File]::WriteAllText($path, $raw, (New-Object Text.UTF8Encoding($false)))\n"
        "  $output = & $operator -Action allowed | Out-String\n"
        "  $results += [pscustomobject]@{ exit_code = $LASTEXITCODE; result = (ConvertFrom-Json -InputObject $output) }\n"
        "}\nConvertTo-Json -InputObject $results -Depth 8 -Compress\nexit 0\n",
        encoding="utf-8")
    response = fixture["run"](probe)
    assert response.returncode == 0, response.stdout + response.stderr
    results = json.loads(response.stdout)
    assert len(results) == len(cases)
    for value, result in zip(cases, results):
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        if valid:
            assert result["exit_code"] == 0, result
            assert result["result"]["client_ids"] == value
        else:
            assert result["exit_code"] != 0, result
            assert result["result"]["status"] == "blocked"


@pytest.mark.parametrize("offset_ms", [None, 100], ids=["old-lifetime", "nearby-lifetime"])
def test_stop_and_status_refuse_a_live_pid_with_a_different_creation_time(protected_operator, offset_ms):
    fixture = protected_operator
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    child = subprocess.Popen([executable, "-c", "import time; time.sleep(45)"])
    try:
        recorded_creation = "2000-01-01T00:00:00+00:00"
        if offset_ms is not None:
            spec = importlib.util.spec_from_file_location(
                "operator_test_process_identity", ROOT / "platforms" / "windows" / "process_identity.py")
            identity = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(identity)
            ticks = identity.creation_time_100ns(child.pid)
            assert ticks is not None
            actual_creation = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
            recorded_creation = (actual_creation + timedelta(milliseconds=offset_ms)).isoformat()
        record = {key: fixture["config"][key] for key in (
            "source_root", "python_executable", "oauth_db", "admin_clients",
            "workspace_root", "candidate_id", "public_origin", "port")}
        record.update(schema_version=1, pid=child.pid, elevated=True,
                      process_executable=executable,
                      started_at=datetime.now(timezone.utc).isoformat(),
                      process_started_at=recorded_creation)
        Path(fixture["config"]["runtime_manifest"]).write_text(json.dumps(record), encoding="utf-8")
        status = fixture["run"](fixture["scripts"] / "status-windows.ps1")
        assert status.returncode != 0
        assert "creation time does not match" in json.loads(status.stdout)["reason"]
        result = fixture["run"](fixture["operator"], "-Action", "stop")
        assert result.returncode != 0
        assert "PID was reused" in json.loads(result.stdout)["reason"]
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)
