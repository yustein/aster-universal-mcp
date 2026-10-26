#!/usr/bin/env python3
"""Private restic/rclone backup orchestration. Never prints credentials or file data.

Explicit init, upload, download verification, conservative retention, and a daily
entrypoint. Exact configuration, receipts, logs, and checkpoints stay root-only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import uuid

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy/archive"
spec = importlib.util.spec_from_file_location("archive_restore_qualifier", REPO / "scripts/archive-restore-qualify.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
TAG = "aster-archive"
HEX = re.compile(r"[0-9a-f]{64}")


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_file(path, *, root=True):
    path = Path(path)
    info = path.lstat()
    if not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("expected a private regular absolute file")
    if root and info.st_uid != 0:
        raise ValueError("expected a root-owned private file")
    return path


def config(path):
    value = json.loads(private_file(path).read_text())
    if value.get("expected_hostname") != socket.gethostname():
        raise ValueError("wrong archive host")
    repository = value["repository"]
    if (not repository.startswith("rclone:ty gdrive:") or "REPLACE" in repository
            or not repository.removeprefix("rclone:ty gdrive:").strip("/")):
        raise ValueError("use the exact dedicated qualified Drive repository")
    root_folder = value.get("drive_root_folder_id", "")
    if "REPLACE" in root_folder or not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", root_folder):
        raise ValueError("use the exact verified private Drive parent folder ID")
    password = private_file(value["password_file"])
    escrow = value["key_escrow"]
    if (password.stat().st_size < 32 or escrow.get("verified") is not True
            or not str(escrow.get("reference", "")).startswith("https://chatgpt.com/library?")
            or escrow.get("password_sha256") != digest(password)):
        raise ValueError("independent recovery-key escrow receipt is missing or mismatched")
    private_file(value["rclone_config"])
    for key in ("restic_program", "rclone_program"):
        binary = Path(value[key])
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("explicit installed executable required")
    for key in ("backup_dir", "state_dir"):
        directory = Path(value[key])
        if not directory.is_absolute() or directory.is_symlink():
            raise ValueError("private absolute directory required")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_uid != 0 or directory.stat().st_mode & 0o077:
            raise ValueError("backup and state directories must be root-private")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value["host"]):
        raise ValueError("invalid restic host label")
    return value


class Offsite:
    def __init__(self, cfg, work, log, recovery_password=None):
        self.cfg, self.work, self.log = cfg, work, log
        self.state = Path(cfg["state_dir"])
        self.checkpoint = {"version": 1, "started_at": utc(), "stage": "preflight"}
        self.env = os.environ.copy()
        for name in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_COMMAND", "RESTIC_REPOSITORY_FILE", "RESTIC_KEY_HINT"):
            self.env.pop(name, None)
        password = private_file(recovery_password or cfg["password_file"])
        self.escrow_copy_tested = recovery_password is not None
        if self.escrow_copy_tested:
            if password.samefile(cfg["password_file"]):
                raise ValueError("recovery proof needs the separately retrieved escrow copy")
            if digest(password) != cfg["key_escrow"]["password_sha256"]:
                raise ValueError("retrieved escrow password does not match its receipt")
        self.env.update(RESTIC_REPOSITORY=cfg["repository"], RESTIC_PASSWORD_FILE=str(password),
                        RESTIC_CACHE_DIR=str(self.state / "cache"), RCLONE_CONFIG=cfg["rclone_config"],
                        RCLONE_DRIVE_ROOT_FOLDER_ID=cfg["drive_root_folder_id"],
                        RCLONE_DRIVE_USE_TRASH="false", RESTIC_READ_CONCURRENCY="2", GOMAXPROCS="2")
        self.base = [cfg["restic_program"], "--retry-lock", "5m", "-o", "rclone.program=" + cfg["rclone_program"]]

    def stage(self, value, **details):
        self.checkpoint.update(stage=value, **details)
        recovery.save(self.work / "state.json", self.checkpoint)
        print(json.dumps({"stage": value, "checkpoint": str(self.work / "state.json")}), flush=True)

    def run(self, argv, *, cwd=None, capture=False, timeout=21600):
        process = subprocess.Popen(argv, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE if capture else self.log, stderr=self.log,
                                   start_new_session=True)
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, argv)
        return output

    def restic(self, *args, capture=False):
        return self.run(self.base + list(args), capture=capture)

    def identity(self, register=False):
        details = json.loads(self.restic("cat", "config", capture=True))
        identity = {"id": details["id"], "version": details["version"],
                    "repository_sha256": hashlib.sha256(self.cfg["repository"].encode()).hexdigest(),
                    "drive_root_folder_id": self.cfg["drive_root_folder_id"]}
        if not HEX.fullmatch(identity["id"]):
            raise ValueError("unexpected repository identity")
        path = self.state / "repository.json"
        if path.exists():
            if json.loads(private_file(path).read_text()) != identity:
                raise ValueError("repository identity changed")
        elif register:
            recovery.save(path, identity)
        else:
            raise ValueError("register the exact repository before use")
        return identity

    def snapshots(self):
        values = json.loads(self.restic("snapshots", "--json", "--host", self.cfg["host"], "--tag", TAG, capture=True)) or []
        if not isinstance(values, list) or any(not HEX.fullmatch(item.get("id", "")) for item in values):
            raise ValueError("unexpected snapshot listing")
        return values

    def anchor(self):
        value = json.loads(private_file(self.state / "anchor.json").read_text())
        if not HEX.fullmatch(value.get("snapshot_id", "")) or value.get("offsite_roundtrip") is not True:
            raise ValueError("no qualified offsite recovery anchor")
        return value

    def upload(self, backup, restore_state=None):
        self.stage("checking_local_backup")
        backup = recovery.checked_backup(Path(backup))
        if backup.parent != Path(self.cfg["backup_dir"]).resolve():
            raise ValueError("upload must be an exact completed backup in the configured directory")
        if {item.name for item in backup.iterdir()} != {"database.dump", "files.tar.gz", "deployment.env", "SHA256SUMS"}:
            raise ValueError("upload directory must contain exactly the four managed backup files")
        proof = None
        if restore_state:
            restore_state = private_file(restore_state)
            local_state = json.loads(restore_state.read_text())
            local_proof_path = private_file(restore_state.parent / "restore-proof.json")
            local_proof = json.loads(local_proof_path.read_text())
            if (local_state.get("stage") not in ("verified", "cleaned") or local_state.get("backup") != str(backup)
                    or local_state.get("backup_manifest_sha256") != digest(backup / "SHA256SUMS")
                    or local_proof.get("result") != "pass" or local_proof.get("original_archive_hash_valid") is not True):
                raise ValueError("actual-corpus restore proof does not match this backup")
            proof = {"state": str(restore_state), "proof_sha256": digest(local_proof_path),
                     "backup_manifest_sha256": local_state["backup_manifest_sha256"],
                     "import_id": local_proof["import_id"], "coverage": local_proof["coverage"]}
        self.stage("uploading_encrypted_backup", backup=str(backup))
        output = self.restic("backup", "--json", "--host", self.cfg["host"], "--tag", TAG, str(backup), capture=True)
        (self.work / "backup-output.jsonl").write_bytes(output)
        summary = [json.loads(line) for line in output.splitlines() if line.strip()]
        candidates = [item["snapshot_id"] for item in summary if item.get("message_type") == "summary" and item.get("snapshot_id")]
        if len(candidates) != 1 or not re.fullmatch(r"[0-9a-f]{8,64}", candidates[0]):
            raise ValueError("no unambiguous completed snapshot receipt")
        matches = [item for item in self.snapshots() if item["id"].startswith(candidates[0])]
        if len(matches) != 1 or matches[0].get("paths") != [str(backup)]:
            raise ValueError("snapshot receipt does not match the uploaded backup")
        snapshot = matches[0]["id"]
        self.stage("checking_repository", snapshot_id=snapshot)
        self.restic("check")
        receipt = {"version": 1, "snapshot_id": snapshot, "backup": str(backup),
                   "manifest_sha256": digest(backup / "SHA256SUMS"), "uploaded_at": utc(),
                   "repository_id": self.identity()["id"], "structural_check": True,
                   "backup_bytes": sum((backup / name).stat().st_size for name in
                                       ("database.dump", "files.tar.gz", "deployment.env", "SHA256SUMS")),
                   "actual_restore_proof": proof, "offsite_roundtrip": False}
        receipts = self.state / "snapshots"
        receipts.mkdir(mode=0o700, exist_ok=True)
        recovery.save(receipts / (snapshot + ".json"), receipt)
        self.stage("uploaded_and_checked", snapshot_id=snapshot)
        return receipt

    def roundtrip(self, snapshot):
        if not HEX.fullmatch(snapshot):
            raise ValueError("use the exact full snapshot ID")
        receipt_path = self.state / "snapshots" / (snapshot + ".json")
        receipt = json.loads(private_file(receipt_path).read_text())
        if receipt["snapshot_id"] != snapshot or receipt["repository_id"] != self.identity()["id"]:
            raise ValueError("snapshot repository mismatch")
        if not self.escrow_copy_tested:
            raise ValueError("roundtrip proof requires a separately retrieved escrow password file")
        if shutil.disk_usage(self.state).free < receipt["backup_bytes"] + recovery.RESERVE:
            raise ValueError("insufficient download reserve")
        target = Path(tempfile.mkdtemp(prefix="download-", dir=self.work))
        marker = target / ".aster-archive-recovery-marker"
        marker.write_text(self.work.name)
        self.stage("restoring_exact_offsite_snapshot", snapshot_id=snapshot, download=str(target))
        self.restic("restore", snapshot, "--target", str(target))
        restored = target / Path(receipt["backup"]).relative_to("/")
        if not restored.resolve(strict=True).is_relative_to(target):
            raise ValueError("downloaded backup escapes its disposable target")
        recovery.checked_backup(restored)
        if digest(restored / "SHA256SUMS") != receipt["manifest_sha256"]:
            raise ValueError("offsite manifest changed")
        receipt.update(offsite_roundtrip=True, escrow_copy_tested=True, roundtrip_at=utc(),
                       roundtrip_checksums_valid=True)
        recovery.save(receipt_path, receipt)
        if receipt.get("actual_restore_proof") and not (self.state / "anchor.json").exists():
            recovery.save(self.state / "anchor.json", receipt)
        if marker.read_text() != self.work.name or target.parent != self.work or target.is_symlink():
            raise ValueError("download cleanup marker mismatch")
        shutil.rmtree(target)
        self.stage("offsite_roundtrip_verified", snapshot_id=snapshot)
        return receipt

    def local_retention(self):
        anchor = self.anchor()
        remote_ids = {item["id"] for item in self.snapshots()}
        if anchor["snapshot_id"] not in remote_ids:
            raise ValueError("qualified recovery anchor is missing")
        rows = []
        for path in (self.state / "snapshots").glob("*.json"):
            row = json.loads(private_file(path).read_text())
            if row["snapshot_id"] in remote_ids and row.get("structural_check"):
                rows.append(row)
        rows.sort(key=lambda item: item["uploaded_at"], reverse=True)
        distinct = list(dict.fromkeys(row["backup"] for row in rows))
        keep = set(distinct[:3]) | {anchor["backup"]}
        removed = 0
        for name in distinct:
            if name in keep:
                continue
            path = Path(name)
            matching = next(row for row in rows if row["backup"] == name)
            if not path.exists():
                continue
            if (path.is_symlink() or path.parent != Path(self.cfg["backup_dir"]).resolve()
                    or not re.fullmatch(r"aster-archive-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]+", path.name)
                    or path.stat().st_uid != 0 or path.stat().st_mode & 0o077):
                raise ValueError("local retention target is not a private managed backup")
            if {item.name for item in path.iterdir()} != {"database.dump", "files.tar.gz", "deployment.env", "SHA256SUMS"}:
                continue  # Preserve any directory the operator has augmented.
            if any(item.is_symlink() or not item.is_file() for item in path.iterdir()):
                raise ValueError("unexpected local backup member")
            if digest(path / "SHA256SUMS") != matching["manifest_sha256"]:
                raise ValueError("local backup manifest changed")
            recovery.checked_backup(path)
            shutil.rmtree(path)
            removed += 1
        return removed

    def daily(self):
        self.anchor()
        pending = self.state / "pending-local-backup.json"
        if pending.exists():
            pending_value = json.loads(private_file(pending).read_text())
            backup = pending_value["backup"]
        else:
            self.stage("checking_local_capacity")
            size = int(self.run(recovery.COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", "postgres",
                                "-Atqc", "SELECT pg_database_size('aster_archive')"], capture=True, timeout=60).strip())
            code = "import os; from pathlib import Path; p=Path(os.environ['ASTER_ARCHIVE_ROOT']); print(sum(f.stat().st_size for f in p.rglob('*') if f.is_file() and not f.is_symlink()))"
            originals = int(self.run(recovery.COMPOSE + ["run", "--rm", "--no-deps", "-T", "backup", "python", "-c", code],
                                    capture=True, timeout=120).strip())
            if shutil.disk_usage(self.cfg["backup_dir"]).free < recovery.RESERVE + 2 * size + originals + 1024**3:
                raise ValueError("scheduled backup storage reserve reached")
            self.stage("creating_consistent_local_backup")
            output = self.run(["bash", str(REPO / "scripts/archive-backup.sh"), self.cfg["backup_dir"]], capture=True).decode()
            lines = [line.removeprefix("Completed local backup: ") for line in output.splitlines()
                     if line.startswith("Completed local backup: ")]
            if len(lines) != 1:
                raise ValueError("local backup did not produce an exact completion receipt")
            backup = lines[0]
            pending_value = {"backup": backup, "created_at": utc()}
            recovery.save(pending, pending_value)
        if pending_value.get("snapshot_id"):
            snapshot = pending_value["snapshot_id"]
            if not HEX.fullmatch(snapshot):
                raise ValueError("invalid pending upload receipt")
            receipt = json.loads(private_file(self.state / "snapshots" / (snapshot + ".json")).read_text())
            if receipt.get("snapshot_id") != snapshot or receipt.get("backup") != backup or not receipt.get("structural_check"):
                raise ValueError("pending backup does not match its uploaded receipt")
        else:
            receipt = self.upload(backup)
            pending_value["snapshot_id"] = receipt["snapshot_id"]
            recovery.save(pending, pending_value)
        removed = self.local_retention()
        pending.unlink()
        return {"snapshot_id": receipt["snapshot_id"], "local_managed_backups_removed": removed}

    def retention(self, apply=False, prune=False):
        anchor = self.anchor()
        snapshots = self.snapshots()
        ids = {item["id"] for item in snapshots}
        if anchor["snapshot_id"] not in ids:
            raise ValueError("qualified recovery anchor is missing")
        protected = {anchor["snapshot_id"]} | {item["id"] for item in sorted(snapshots, key=lambda s: s["time"], reverse=True)[:2]}
        self.stage("planning_retention")
        plan = json.loads(self.restic("forget", "--dry-run", "--json", "--group-by", "host", "--host", self.cfg["host"],
                          "--tag", TAG, "--keep-last", "2", "--keep-daily", "7", "--keep-weekly", "4",
                          "--keep-monthly", "6", capture=True)) or []
        remove = set()
        for group in plan:
            if not isinstance(group, dict) or "keep" not in group:
                raise ValueError("unexpected retention plan")
            for item in group.get("remove") or []:
                identity = item.get("id", "")
                if not HEX.fullmatch(identity) or identity not in ids:
                    raise ValueError("retention plan escapes this backup set")
                if identity not in protected:
                    remove.add(identity)
        recovery.save(self.work / "retention-plan.json", {"remove": sorted(remove), "protected": sorted(protected)})
        if apply and (remove or prune):
            if len(ids - remove) < 2:
                if remove:
                    raise ValueError("retention would leave fewer than two recovery points")
            self.restic("check")
            if remove:
                self.restic("forget", *sorted(remove))
            remaining = {item["id"] for item in self.snapshots()}
            if not protected <= remaining:
                raise ValueError("protected recovery point missing after retention")
            if prune:
                self.restic("prune")
            self.restic("check")
        return {"planned_removals": len(remove), "applied": bool(apply and remove),
                "protected_recovery_points": len(protected), "pruned": bool(apply and prune)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--recovery-password-file", type=Path)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("init")
    sub.add_parser("register")
    upload = sub.add_parser("upload")
    upload.add_argument("--backup", type=Path, required=True)
    upload.add_argument("--restore-state", type=Path)
    roundtrip = sub.add_parser("roundtrip")
    roundtrip.add_argument("--snapshot", required=True)
    sub.add_parser("daily")
    retention = sub.add_parser("retention")
    retention.add_argument("--apply", action="store_true")
    retention.add_argument("--prune", action="store_true")
    integrity = sub.add_parser("integrity")
    integrity.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("run as root so backup files and credentials stay private")
    os.umask(0o077)
    cfg = config(args.config)
    state_dir = Path(cfg["state_dir"])
    with (state_dir / "offsite.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reboot_marker = state_dir / "reboot-pending.json"
        if reboot_marker.exists():
            marker = json.loads(private_file(reboot_marker).read_text())
            if datetime.fromisoformat(marker["expires_at"]) > datetime.now(timezone.utc):
                raise ValueError("a qualified host reboot is pending; retry after its deadline")
        work = Path(tempfile.mkdtemp(prefix="run-", dir=state_dir))
        with (work / "operations.log").open("ab") as log:
            runner = Offsite(cfg, work, log, args.recovery_password_file)
            try:
                runner.stage(args.mode)
                if args.mode == "init":
                    runner.restic("init", "--repository-version", "2")
                    runner.identity(register=True)
                    result = {"repository_registered": True}
                elif args.mode == "register":
                    runner.identity(register=True)
                    result = {"repository_registered": True}
                else:
                    runner.identity()
                    if args.mode == "upload":
                        receipt = runner.upload(args.backup, args.restore_state)
                        result = {"snapshot_id": receipt["snapshot_id"], "structural_check": True}
                    elif args.mode == "roundtrip":
                        receipt = runner.roundtrip(args.snapshot)
                        result = {"snapshot_id": receipt["snapshot_id"], "offsite_roundtrip": True,
                                  "escrow_copy_tested": True, "linked_actual_restore_proof": bool(receipt["actual_restore_proof"])}
                    elif args.mode == "daily":
                        result = runner.daily()
                    elif args.mode == "retention":
                        result = runner.retention(args.apply, args.prune)
                    else:
                        runner.restic("check", *(["--read-data"] if args.full else []))
                        result = {"repository_checked": True, "all_repository_data_read": args.full}
                runner.stage("complete")
                print(json.dumps({"result": "pass", **result, "checkpoint": str(work / "state.json")}), flush=True)
            except Exception as exc:
                runner.checkpoint.update(error_type=type(exc).__name__, failed_at=utc())
                recovery.save(work / "state.json", runner.checkpoint)
                print(json.dumps({"result": "fail", "stage": runner.checkpoint["stage"], "error_type": type(exc).__name__,
                                  "checkpoint": str(work / "state.json")}), flush=True)
                return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"result": "fail", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
