"""Local-only operator CLI for pairing codes; never expose as an MCP tool."""
import argparse
import os
from pathlib import Path
from pairing import PairingStore

DEFAULT_DB = os.environ.get("ASTER_MCP_OAUTH_DB", "/var/lib/aster-universal-mcp/oauth.sqlite")

def main():
    parser = argparse.ArgumentParser(description="Manage local owner pairing codes")
    parser.add_argument("--db", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("--ttl", type=int, default=300)
    sub.add_parser("revoke")
    args = parser.parse_args()
    if os.name == "posix" and os.geteuid() != 0:
        parser.error("run this local operator command as root")
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.parent.chmod(0o700)
    store = PairingStore(str(path))
    if os.name == "posix":
        path.chmod(0o600)
    if args.command == "issue":
        code = store.issue(ttl_seconds=args.ttl)
        print("ONE-TIME CODE (keep private; expires in", args.ttl, "seconds):")
        print(code)
        print("Code is NOT an MCP access token; it must be redeemed by the future OAuth pairing flow.")
    else:
        store.revoke()
        print("Outstanding pairing code revoked.")

if __name__ == "__main__":
    main()
