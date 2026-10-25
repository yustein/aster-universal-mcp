"""Local operator enrollment, delegation grants and observed receiver evidence.

Never expose this module as an MCP tool. Run with access to protected coordinator
state. OAuth registration and owner approval remain the existing Aster flow.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from tool_access import read_admin_allowlist
from . import CONTRACT_VERSION
from .store import CoordinationError, Store


def registered_client(database: Path, client_id: str):
    """Read a preexisting OAuth registry; never initialize or repair it here."""
    if not database.is_absolute() or not database.is_file():
        raise ValueError("provide an existing absolute OAuth database path")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("""SELECT c.client_id,c.name,c.redirect_uris,c.revoked
            FROM oauth_clients c JOIN client_metadata m ON m.client_id=c.client_id
            WHERE c.client_id=?""", (client_id,)).fetchone()
    if not row or row[3]:
        raise ValueError("exact OAuth client is not registered and active")
    return {"client_id": row[0], "name": row[1], "redirect_uris": json.loads(row[2])}


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--db", type=Path, required=True, help="Separate protected coordination database")
    sub = command.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="Initialize new empty coordination state; no OAuth grants")
    sub.add_parser("agents", help="Inspect local enrollment and bindings")
    enroll = sub.add_parser("enroll", help="Map one approved OAuth client to one logical agent")
    enroll.add_argument("--oauth-db", type=Path, required=True)
    enroll.add_argument("--allowlist", type=Path, required=True)
    enroll.add_argument("--agent-id", required=True)
    enroll.add_argument("--client-id", required=True)
    enroll.add_argument("--name", required=True)
    enroll.add_argument("--provider", required=True)
    enroll.add_argument("--receive-mode", choices=("pull", "native"), default="pull")
    enroll.add_argument("--capability", action="append", default=[])
    enroll.add_argument("--replace", action="store_true", help="Explicitly replace identity and fence old running claims")
    for name in ("grant", "revoke-route"):
        action = sub.add_parser(name)
        action.add_argument("sender")
        action.add_argument("receiver")
    for name in ("disable", "binding"):
        sub.add_parser(name).add_argument("agent")
    verify = sub.add_parser("verify-receiver", help="Record actual host-observed receipt in the intended session")
    verify.add_argument("agent")
    verify.add_argument("--subscription-id", required=True)
    verify.add_argument("--observed-session-id", required=True)
    verify.add_argument("--evidence-ref", required=True,
                        help="Reference to the independently inspected host execution/probe, not a declared alias")
    return command


def run(args):
    if not args.db.is_absolute():
        raise ValueError("coordination database must use an absolute path")
    if args.action == "enroll":
        if args.db.resolve() == args.oauth_db.resolve():
            raise ValueError("coordination and OAuth state must use separate databases")
        registration = registered_client(args.oauth_db, args.client_id)
        if not args.allowlist.is_absolute() or args.client_id not in read_admin_allowlist(args.allowlist):
            raise ValueError("exact client must already have local administrator approval")
    store = Store(args.db)
    if args.action == "init":
        return {"contract": CONTRACT_VERSION, "initialized": True, "grants_created": False}
    if args.action == "enroll":
        result = store.register_agent(agent_id=args.agent_id, client_id=args.client_id,
                                      display_name=args.name, provider=args.provider,
                                      receive_mode=args.receive_mode, capabilities=args.capability,
                                      replace=args.replace)
        return {"agent": result, "checked_registration": registration}
    if args.action in ("grant", "revoke-route"):
        return store.grant(sender=args.sender, receiver=args.receiver, allowed=args.action == "grant")
    if args.action == "disable":
        return store.disable_agent(args.agent)
    if args.action == "binding":
        return {"binding": store.native_binding(args.agent)}
    if args.action == "verify-receiver":
        return store.verify_receiver(agent_id=args.agent, subscription_id=args.subscription_id,
                                     observed_session_id=args.observed_session_id,
                                     evidence_ref=args.evidence_ref)
    if args.action == "agents":
        with closing(sqlite3.connect(str(store.path))) as db:
            agents = [json.loads(row[0]) for row in db.execute("SELECT document FROM coord_agents ORDER BY agent_id")]
        # Exact client IDs are intentionally visible to the LOCAL operator.
        return {"agents": [{**agent, "native_binding": store.native_binding(agent["agent_id"])} for agent in agents]}
    raise ValueError("unsupported operator action")


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    try:
        result = run(args)
    except (CoordinationError, ValueError) as error:
        command.error(str(error))
    except (OSError, sqlite3.Error):
        command.error("protected state is unavailable; inspect local configuration")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
