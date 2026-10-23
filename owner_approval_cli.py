"""Local-only approval CLI, intentionally NOT a public OAuth endpoint.

The owner must execute this on the host (or through an independently
authenticated administrator channel). Never expose it as an MCP tool.
"""
import argparse
import json
import os
from approval_store import ApprovalStore
from pairing import PairingStore

def main():
    parser=argparse.ArgumentParser(description="Locally approve pending MCP OAuth requests")
    parser.add_argument("--db",default="/var/lib/aster-universal-mcp/oauth.sqlite")
    sub=parser.add_subparsers(dest="command",required=True)
    inspect=sub.add_parser("inspect")
    inspect.add_argument("request_id")
    approve=sub.add_parser("approve")
    approve.add_argument("request_id")
    approve.add_argument("--code",required=True,help="single-use local pairing code")
    args=parser.parse_args()
    if os.name=="posix" and os.geteuid()!=0:
        parser.error("owner approval requires local root context")
    approvals=ApprovalStore(args.db)
    record=approvals.get(args.request_id)
    if record is None:
        parser.error("unknown or expired request")
    if args.command=="inspect":
        print(json.dumps({k:record[k] for k in ("client_id","redirect_uri","resource","scope","expires_at")},indent=2))
        return
    if record["scope"]!="system:admin":
        parser.error("unexpected scope; only system:admin is enabled")
    if not PairingStore(args.db).consume(args.code):
        parser.error("invalid or expired pairing code")
    if not approvals.approve(args.request_id):
        parser.error("request already approved or expired")
    print("Owner approved privileged OAuth request",args.request_id)

if __name__=="__main__":
    main()
