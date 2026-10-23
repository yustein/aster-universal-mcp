"""Operator-only local pairing code management; do not expose as MCP tools."""
import argparse
import os
from pairing import PairingStore

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["issue", "revoke"])
    parser.add_argument("--ttl", type=int, default=300)
    args = parser.parse_args()
    db = os.environ.get("ASTER_PAIRING_DB", os.environ.get("ASTER_MCP_OAUTH_DB", "/var/lib/aster-universal-mcp/oauth.sqlite"))
    store = PairingStore(db)
    if args.action == "issue":
        print("One-time owner pairing code (show locally; never paste in ChatGPT):")
        print(store.issue(args.ttl))
    else:
        store.revoke()
        print("Pending pairing revoked.")

if __name__ == "__main__":
    main()
