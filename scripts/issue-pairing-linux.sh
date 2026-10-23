#!/usr/bin/env bash
# Local owner-only pairing command. No network endpoint.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${ASTER_MCP_OAUTH_DB:-/var/lib/aster-universal-mcp/oauth.sqlite}"
PY="${ASTER_MCP_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -f "$DB" ]; then echo "ERROR: OAuth DB missing at $DB" >&2; exit 2; fi
if [ ! -x "$PY" ]; then echo "ERROR: Python virtualenv missing" >&2; exit 3; fi
"$PY" "$ROOT/pairing_admin.py" --db "$DB" issue --ttl 900
echo "Copy this short code into Claude pairing within 15 minutes."
