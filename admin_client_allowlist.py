"""Operator-only OAuth client allowlist management. Never export as MCP tools.

Full remote administration requires BOTH an owner-paired system:admin OAuth
token and an explicit client-id allowlist entry created locally.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def _registered(db_path: Path, client_id: str) -> bool:
    try:
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT revoked FROM oauth_clients WHERE client_id=?", (client_id,)
            ).fetchone()
            meta = db.execute(
                "SELECT 1 FROM client_metadata WHERE client_id=?", (client_id,)
            ).fetchone()
    except sqlite3.OperationalError:
        return False  # Missing tables are not proof of a registered client.
    return bool(row is not None and not row[0] and meta)


def _read_allowlist(path: Path) -> list[str]:
    if path.is_symlink():
        raise PermissionError("refusing symlink allowlist")
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("invalid allowlist")
    return value


def update_allowlist(path: Path, client_id: str, *, approve: bool, db: Path) -> list[str]:
    if not path.is_absolute() or not db.is_absolute():
        raise ValueError("absolute paths required")
    if not client_id or len(client_id) > 256:
        raise ValueError("invalid client ID")
    if approve and not _registered(db, client_id):
        raise PermissionError("client is not registered and active in OAuth database")
    values = _read_allowlist(path)
    updated = sorted(set(values) | {client_id}) if approve else sorted(
        set(values) - {client_id}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".aster-clients-", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(updated, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return updated


def main():
    parser = argparse.ArgumentParser(description="Manage explicitly authorized OAuth administration clients")
    parser.add_argument("--db", type=Path, required=True, help="OAuth SQLite DB path")
    parser.add_argument("--allowlist", type=Path, required=True, help="Operator-protected JSON path")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("clients", help="List active registered OAuth clients")
    sub.add_parser("allowed", help="List administrator client IDs")
    for name in ("approve", "revoke"):
        sub.add_parser(name).add_argument("client_id")
    args = parser.parse_args()
    if args.action == "clients":
        with sqlite3.connect(args.db) as db:
            entries = db.execute(
                "SELECT client_id, name, redirect_uris FROM oauth_clients WHERE revoked=0"
            ).fetchall()
        for cid, name, redirects in entries:
            print(f"{cid}\t{name}\t{redirects}")
    elif args.action == "allowed":
        for cid in _read_allowlist(args.allowlist):
            print(cid)
    else:
        update_allowlist(args.allowlist, args.client_id,
                         approve=args.action == "approve", db=args.db)
        print("Administrator authorization updated; verify owner and OAuth pairing.")


if __name__ == "__main__":
    main()
