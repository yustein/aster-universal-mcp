#!/usr/bin/env python3
"""Read-only pre/post host reboot evidence. This program never reboots or starts services."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_restore_qualifier", REPO / "scripts/archive-restore-qualify.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
ENRICHMENT_SERVICES = ("embedding", "knowledge", "cognee")


def run(argv, *, timeout=60):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=True).stdout.decode().strip()


def private_json(path):
    path = Path(path)
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("checkpoint must be a root-private regular file")
    return json.loads(path.read_text())


def boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    uuid.UUID(value)
    return value


def boot_time():
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return datetime.fromtimestamp(int(line.split()[1]), timezone.utc)
    raise ValueError("host boot timestamp is unavailable")


def unit_state(names):
    result = {}
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", name):
            raise ValueError("use exact service unit names")
        result[name] = {"active": run(["systemctl", "is-active", name]),
                        "enabled": run(["systemctl", "is-enabled", name])}
        if result[name] != {"active": "active", "enabled": "enabled"}:
            raise ValueError("required service is not active and persistently enabled")
    return result


def containers():
    result = {}
    configured = set(run(recovery.COMPOSE + ["config", "--services"]).splitlines())
    template = "{{.Id}}|{{.Image}}|{{.Name}}|{{.State.Status}}|{{.HostConfig.RestartPolicy.Name}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{json .Mounts}}"
    for service in ("db", "watch", "worker", "mcp", *ENRICHMENT_SERVICES):
        optional = service in ("mcp", *ENRICHMENT_SERVICES)
        if optional and service not in configured:
            continue
        identities = run(recovery.COMPOSE + ["ps", "--all", "--quiet", service]).splitlines()
        if not identities and optional:
            continue
        if len(identities) != 1:
            raise ValueError("archive service must have exactly one persistent container")
        row = run(["docker", "inspect", "--format", template, identities[0]]).split("|", 6)
        identity, image, name, status, restart, health, mounts = row
        if service == "mcp" and status != "running":
            continue  # Publishing MCP remains a separate deployment choice.
        if status != "running" or restart != "unless-stopped" or (service == "db" and health != "healthy"):
            raise ValueError("archive service is not healthy with persistent restart policy")
        result[service] = {"id": identity, "image": image, "name": name.lstrip("/"), "status": status,
                           "restart": restart, "health": health,
                           "volumes": {item["Destination"]: item["Name"] for item in json.loads(mounts)
                                       if item["Type"] == "volume"}}
    return result


def volume_identities(services):
    names = {name for service in services.values() for name in service["volumes"].values()}
    result = {}
    for name in sorted(names):
        parts = run(["docker", "volume", "inspect", "--format", "{{.Name}}|{{.CreatedAt}}|{{.Driver}}", name]).split("|", 2)
        result[name] = {"name": parts[0], "created_at": parts[1], "driver": parts[2]}
    return result


def other_containers(services):
    known = {row["name"] for row in services.values()}
    rows = run(["docker", "ps", "--format", "{{.ID}}|{{.Names}}"])
    return {name: identity for identity, name in (row.split("|", 1) for row in rows.splitlines()) if name not in known}


def vm_state():
    base = ["virsh", "--connect", "qemu:///system", "list"]
    running = run(base + ["--name", "--state-running"]).splitlines()
    autostart = run(base + ["--all", "--name", "--autostart"]).splitlines()
    return {"running": running, "autostart": autostart}


def probe(owner, source, import_id, query, enrichment=()):
    flags = ["--check-" + service for service in enrichment]
    result = json.loads(run(recovery.COMPOSE + ["run", "--rm", "--no-deps", "-T", "mcp", "python", "-c",
                         recovery.PROBE.read_text(), "capture", "--owner", owner, "--source", source,
                         "--import-id", import_id, "--query", query, *flags], timeout=300))
    if result.get("result") != "pass":
        raise ValueError("reader proof failed")
    result.pop("database_bytes", None)  # Physical catalog sizes can change across a clean restart.
    return result


def compare_probes(before, after):
    """Canonical evidence stays exact; unfinished derived indexes may advance."""
    if {k: v for k, v in before.items() if k != "enrichment"} != {
            k: v for k, v in after.items() if k != "enrichment"}:
        raise ValueError("canonical reader proof differs from the reboot checkpoint")
    previous, current = before.get("enrichment", {}), after.get("enrichment", {})
    if set(previous) != set(current):
        raise ValueError("enabled enrichment evidence changed")
    rules = {
        "embedding": (("generation_id", "current_passages", "unique_texts"),
                      ("embedded_passages", "embedded_unique_texts"), "failed_unique_texts", "semantic_results",
                      ("passage_hash_valid", "cross_owner_denied")),
        "knowledge": (("pipeline_key",), ("records", "completed_revisions"), "failed_revisions", "search_results",
                      ("exact_citation_valid", "passage_hash_valid", "cross_owner_denied")),
        "cognee": (("pipeline_key",), ("completed_passages", "derived_summaries"), "failed_passages", None,
                   ("graph_is_not_authoritative",)),
    }
    for name, old in previous.items():
        if name not in rules:
            raise ValueError("unknown enrichment proof")
        new = current[name]
        equal, increasing, failures, hits, booleans = rules[name]
        if (set(new) != set(old) or any(old[key] != new[key] for key in equal)
                or any(new[key] < old[key] for key in increasing)
                or new[failures] > old[failures]
                or hits is not None and not 1 <= new[hits] <= 3
                or any(new[key] is not True for key in booleans)):
            raise ValueError("enrichment data or readiness did not recover")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("pre", "post"))
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--expected-hostname", default="tony-Legion-Y540-15IRH")
    parser.add_argument("--owner", default="owner-personal")
    parser.add_argument("--source", default="chatgpt-personal")
    parser.add_argument("--import-id")
    parser.add_argument("--query", default="ChatGPT")
    parser.add_argument("--unit", action="append", default=[], help="exact enabled RDC/control/tunnel service name")
    parser.add_argument("--require-enrichment", choices=ENRICHMENT_SERVICES, action="append", default=[],
                        help="fail preflight unless this persistent enrichment service is running")
    parser.add_argument("--offsite-state-dir", type=Path, default=Path("/var/lib/aster-archive-backup"))
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != args.expected_hostname:
        raise ValueError("run as root on the explicitly qualified native archive host")
    os.umask(0o077)
    marker_path = args.offsite_state_dir / "reboot-pending.json"
    with (args.offsite_state_dir / "offsite.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.mode == "pre":
            if args.checkpoint.exists() or not args.import_id:
                raise ValueError("use a new checkpoint path and completed import ID")
            args.checkpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            state = {"version": 1, "token": uuid.uuid4().hex, "stage": "preflight",
                     "created_at": datetime.now(timezone.utc).isoformat(), "hostname": socket.gethostname(),
                     "before_boot_id": boot_id(), "owner": args.owner, "source": args.source,
                     "import_id": args.import_id, "query": args.query}
        else:
            state = private_json(args.checkpoint)
            if state["hostname"] != socket.gethostname() or state["stage"] not in ("ready_for_manual_reboot", "postflight_failed"):
                raise ValueError("checkpoint is not a prepared reboot on this host")
        try:
            if args.mode == "pre":
                state["stage"] = "recovery_anchor"
                anchor = private_json(args.offsite_state_dir / "anchor.json")
                if not (anchor.get("offsite_roundtrip") and anchor.get("escrow_copy_tested") and anchor.get("actual_restore_proof")):
                    raise ValueError("actual restore and independent escrow-key recovery must pass first")
                state["offsite_snapshot_id"] = anchor["snapshot_id"]
                state["stage"] = "control_services"
                state["units"] = unit_state(list(dict.fromkeys(["docker.service", *args.unit])))
                state["stage"] = "archive_services"
                state["services"] = containers()
                if not set(args.require_enrichment) <= set(state["services"]):
                    raise ValueError("a required enrichment service is not running")
                state["enrichment"] = [name for name in ENRICHMENT_SERVICES if name in state["services"]]
                state["volumes"] = volume_identities(state["services"])
                state["other_running_containers"] = other_containers(state["services"])
                state["stage"] = "vm_autostart_guard"
                state["vms"] = vm_state()
                if state["vms"]["running"] or state["vms"]["autostart"]:
                    raise ValueError("retired VMs must remain stopped across this reboot")
                state["stage"] = "active_import_guard"
                running = run(recovery.COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", "aster_archive",
                              "-Atqc", "SELECT count(*) FROM aster_archive.imports WHERE status='running'"])
                if running != "0":
                    raise ValueError("complete or pause the active import before a qualification reboot")
                state["stage"] = "baseline_reader_proof"
                state["baseline"] = probe(args.owner, args.source, args.import_id, args.query, state["enrichment"])
                state["reboot_before"] = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
                state["stage"] = "ready_for_manual_reboot"
                recovery.save(args.checkpoint, state)
                recovery.save(marker_path, {"token": state["token"],
                                            "expires_at": (datetime.fromisoformat(state["reboot_before"]) + timedelta(minutes=10)).isoformat(),
                                            "checkpoint": str(args.checkpoint.resolve())})
                print(json.dumps({"result": "pass", "stage": state["stage"], "checkpoint": str(args.checkpoint),
                                  "reboot_before": state["reboot_before"], "automatic_reboot_issued": False,
                                  "archive_service_count": len(state["services"]),
                                  "enrichment_readiness_checked": state["enrichment"],
                                  "other_running_containers": list(state["other_running_containers"]),
                                  "verified_control_units": list(state["units"])}), flush=True)
            else:
                stage = "reboot_checkpoint_binding"
                marker = private_json(marker_path)
                if marker.get("token") != state["token"] or marker.get("checkpoint") != str(args.checkpoint.resolve()):
                    raise ValueError("reboot marker does not belong to this checkpoint")
                stage = "changed_host_boot_id"
                state["after_boot_id"] = boot_id()
                if state["after_boot_id"] == state["before_boot_id"]:
                    raise ValueError("the host has not rebooted")
                observed_boot = boot_time()
                if not (datetime.fromisoformat(state["created_at"]) <= observed_boot
                        <= datetime.fromisoformat(state["reboot_before"]) + timedelta(minutes=3)):
                    raise ValueError("host reboot falls outside the prepared window")
                state["after_boot_time"] = observed_boot.isoformat()
                stage = "automatic_service_recovery"
                current_units = unit_state(list(state["units"]))
                current_services = containers()
                if current_services != state["services"]:
                    raise ValueError("archive containers changed or did not recover automatically")
                if volume_identities(current_services) != state["volumes"]:
                    raise ValueError("persistent volume identity changed")
                current_other = other_containers(current_services)
                if any(current_other.get(name) != identity for name, identity in state["other_running_containers"].items()):
                    raise ValueError("another previously running container has not recovered")
                stage = "vm_recovery_guard"
                if vm_state() != state["vms"]:
                    raise ValueError("VM state changed across reboot")
                stage = "reader_recovery_proof"
                observed = probe(state["owner"], state["source"], state["import_id"], state["query"], state.get("enrichment", []))
                compare_probes(state["baseline"], observed)
                state["postflight_reader_proof"] = observed
                state.update(stage="reboot_recovery_verified", completed_at=datetime.now(timezone.utc).isoformat())
                recovery.save(args.checkpoint, state)
                if marker_path.exists() and private_json(marker_path).get("token") == state["token"]:
                    marker_path.unlink()
                print(json.dumps({"result": "pass", "stage": state["stage"], "checkpoint": str(args.checkpoint),
                                  "host_boot_id_changed": True, "same_persistent_volumes": True,
                                  "same_containers_running_after_reboot": True, "restart_policy_verified": True,
                                  "reader_counts_hashes_and_scope_denial_valid": True,
                                  "control_units_active_and_enabled": list(current_units),
                                  "enrichment_readiness": observed.get("enrichment", {}),
                                  "coverage": observed["coverage"]}), flush=True)
        except Exception as exc:
            if args.mode == "post":
                state.update(stage="postflight_failed", failure_stage=stage)
            state["error_type"] = type(exc).__name__
            recovery.save(args.checkpoint, state)
            print(json.dumps({"result": "fail", "stage": state["stage"], "failure_stage": state.get("failure_stage"),
                              "error_type": type(exc).__name__, "checkpoint": str(args.checkpoint)}), flush=True)
            return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"result": "fail", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
