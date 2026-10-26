"""Reboot verification cannot confuse a process restart with a host reboot."""
import importlib.util
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_reboot_check", ROOT / "scripts/archive-reboot-check.py")
reboot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reboot)


def test_unit_names_are_literal_service_identifiers(monkeypatch):
    monkeypatch.setattr(reboot, "run", lambda *a, **k: pytest.fail("unsafe unit must not execute"))
    with pytest.raises(ValueError):
        reboot.unit_state(["docker.service; reboot"])


def post_setup(tmp_path, monkeypatch):
    path = tmp_path / "reboot.json"
    state = {"version": 1, "token": "synthetic-token", "hostname": "synthetic-host",
             "stage": "ready_for_manual_reboot", "before_boot_id": "old-boot",
             "created_at": "2026-10-26T12:00:00+00:00", "reboot_before": "2026-10-26T12:15:00+00:00",
             "units": {"docker.service": {"active": "active", "enabled": "enabled"}},
             "services": {"db": {"id": "db-id"}}, "volumes": {"db-volume": "same-created-at"},
             "other_running_containers": {"unrelated-agent": "same-id"},
             "vms": {"running": [], "autostart": []},
             "owner": "synthetic-owner", "source": "synthetic-source", "import_id": "synthetic-import",
             "query": "synthetic", "baseline": {"coverage": {"conversations": 1}}}
    reboot.recovery.save(path, state)
    reboot.recovery.save(tmp_path / "reboot-pending.json", {"token": state["token"], "checkpoint": str(path.resolve())})
    monkeypatch.setattr(reboot.os, "geteuid", lambda: 0)
    monkeypatch.setattr(reboot.socket, "gethostname", lambda: "synthetic-host")
    monkeypatch.setattr(reboot, "boot_time", lambda: datetime(2026, 10, 26, 12, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(reboot, "private_json", lambda p: json.loads(Path(p).read_text()))
    monkeypatch.setattr(reboot.sys, "argv", ["archive-reboot-check.py", "post", "--checkpoint", str(path),
                         "--expected-hostname", "synthetic-host", "--offsite-state-dir", str(tmp_path)])
    return path, state


def test_same_boot_id_cannot_pass_or_start_services(tmp_path, monkeypatch, capsys):
    path, state = post_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(reboot, "boot_id", lambda: state["before_boot_id"])
    monkeypatch.setattr(reboot, "run", lambda *a, **k: pytest.fail("must not attempt repair"))
    assert reboot.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["failure_stage"] == "changed_host_boot_id"
    assert json.loads(path.read_text())["stage"] == "postflight_failed"


def test_post_requires_other_agents_recover_and_never_repairs_them(tmp_path, monkeypatch, capsys):
    path, state = post_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(reboot, "boot_id", lambda: "new-boot")
    monkeypatch.setattr(reboot, "unit_state", lambda names: state["units"])
    monkeypatch.setattr(reboot, "containers", lambda: state["services"])
    monkeypatch.setattr(reboot, "volume_identities", lambda services: state["volumes"])
    monkeypatch.setattr(reboot, "other_containers", lambda services: {})
    monkeypatch.setattr(reboot, "run", lambda *a, **k: pytest.fail("must not repair unrelated services"))
    assert reboot.main() == 1
    assert json.loads(capsys.readouterr().out)["failure_stage"] == "automatic_service_recovery"


def test_mismatched_reboot_marker_cannot_pass(tmp_path, monkeypatch, capsys):
    path, state = post_setup(tmp_path, monkeypatch)
    marker = tmp_path / "reboot-pending.json"
    reboot.recovery.save(marker, {"token": "another-checkpoint"})
    monkeypatch.setattr(reboot, "boot_id", lambda: "new-boot")
    assert reboot.main() == 1
    assert json.loads(capsys.readouterr().out)["failure_stage"] == "reboot_checkpoint_binding"
    assert marker.exists()


def test_stale_reboot_checkpoint_cannot_qualify_later_boot(tmp_path, monkeypatch, capsys):
    path, state = post_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(reboot, "boot_id", lambda: "new-boot")
    monkeypatch.setattr(reboot, "boot_time", lambda: datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert reboot.main() == 1
    assert json.loads(capsys.readouterr().out)["failure_stage"] == "changed_host_boot_id"


def test_pass_clears_matching_reboot_marker(tmp_path, monkeypatch, capsys):
    path, state = post_setup(tmp_path, monkeypatch)
    marker = tmp_path / "reboot-pending.json"
    monkeypatch.setattr(reboot, "boot_id", lambda: "new-boot")
    monkeypatch.setattr(reboot, "unit_state", lambda names: state["units"])
    monkeypatch.setattr(reboot, "containers", lambda: state["services"])
    monkeypatch.setattr(reboot, "volume_identities", lambda services: state["volumes"])
    monkeypatch.setattr(reboot, "other_containers", lambda services: state["other_running_containers"])
    monkeypatch.setattr(reboot, "vm_state", lambda: state["vms"])
    monkeypatch.setattr(reboot, "probe", lambda *a: state["baseline"])
    monkeypatch.setattr(reboot, "run", lambda *a, **k: pytest.fail("must not start or stop services"))
    assert reboot.main() == 0
    assert json.loads(capsys.readouterr().out)["host_boot_id_changed"] is True
    assert not marker.exists()
    assert json.loads(path.read_text())["stage"] == "reboot_recovery_verified"


@pytest.mark.parametrize("enrichment", [False, True])
def test_container_inventory_includes_enrichment_only_when_configured(monkeypatch, enrichment):
    base = ["db", "watch", "worker"]
    names = base + (["embedding", "knowledge", "cognee"] if enrichment else [])
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        if argv[-2:] == ["config", "--services"]:
            return "\n".join(names + ["mcp"])
        if "--quiet" in argv:
            return "" if argv[-1] == "mcp" else argv[-1] + "-id"
        if argv[:2] == ["docker", "inspect"]:
            name = argv[-1].removesuffix("-id")
            return "|".join([name + "-id", "image-id", "/" + name, "running", "unless-stopped",
                             "healthy" if name == "db" else "", "[]"])
        pytest.fail("unexpected non-read-only command")
    monkeypatch.setattr(reboot, "run", fake)
    assert set(reboot.containers()) == set(names)
    assert not any("start" in argv or "stop" in argv for argv in calls)


def test_existing_failed_enrichment_container_is_not_silently_optional(monkeypatch):
    def fake(argv, **kwargs):
        if argv[-2:] == ["config", "--services"]:
            return "db\nwatch\nworker\nembedding"
        if "--quiet" in argv:
            return argv[-1] + "-id"
        name = argv[-1].removesuffix("-id")
        return "|".join([name + "-id", "image", "/" + name, "exited" if name == "embedding" else "running",
                         "unless-stopped", "healthy" if name == "db" else "", "[]"])
    monkeypatch.setattr(reboot, "run", fake)
    with pytest.raises(ValueError, match="not healthy"):
        reboot.containers()


def enrichment_evidence():
    return {"coverage": {"conversations": 1}, "enrichment": {
        "embedding": {"generation_id": "pinned-model", "current_passages": 10, "unique_texts": 8,
                      "embedded_passages": 3, "embedded_unique_texts": 2, "failed_unique_texts": 0,
                      "semantic_results": 2, "passage_hash_valid": True, "cross_owner_denied": True},
        "knowledge": {"pipeline_key": "extractive", "records": 2, "completed_revisions": 1,
                      "failed_revisions": 0, "search_results": 1, "exact_citation_valid": True,
                      "passage_hash_valid": True, "cross_owner_denied": True},
        "cognee": {"pipeline_key": "pilot", "completed_passages": 1, "derived_summaries": 0,
                   "failed_passages": 0, "graph_is_not_authoritative": True}}}


def test_reboot_comparison_allows_progress_and_partial_coverage():
    before = enrichment_evidence()
    after = deepcopy(before)
    after["enrichment"]["embedding"].update(embedded_passages=5, embedded_unique_texts=4, semantic_results=3)
    after["enrichment"]["knowledge"].update(records=4, completed_revisions=2, search_results=2)
    after["enrichment"]["cognee"].update(completed_passages=2, derived_summaries=1)
    reboot.compare_probes(before, after)


@pytest.mark.parametrize("component,key,value", [
    ("embedding", "generation_id", "changed-model"),
    ("embedding", "embedded_passages", 2),
    ("embedding", "semantic_results", 0),
    ("knowledge", "exact_citation_valid", False),
    ("knowledge", "failed_revisions", 1),
    ("cognee", "completed_passages", 0),
])
def test_reboot_rejects_lost_index_wrong_model_or_failed_retrieval(component, key, value):
    before = enrichment_evidence()
    after = deepcopy(before)
    after["enrichment"][component][key] = value
    with pytest.raises(ValueError, match="enrichment"):
        reboot.compare_probes(before, after)


def test_new_derived_work_cannot_mask_changed_canonical_evidence():
    before = enrichment_evidence()
    after = deepcopy(before)
    after["coverage"]["conversations"] = 0
    with pytest.raises(ValueError, match="canonical"):
        reboot.compare_probes(before, after)
