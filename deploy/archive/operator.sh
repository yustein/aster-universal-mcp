#!/bin/sh
# Local owner terminal only. Keep this one-shot procedure outside the MCP API.
set -eu
cd "$(dirname "$0")"

case "${1:-}" in
  issue|revoke|inspect|approve) ;;
  *) printf '%s\n' 'Usage: ./operator.sh issue [--ttl SECONDS] | revoke | inspect REQUEST_ID | approve REQUEST_ID' >&2; exit 2 ;;
esac

running=$(docker compose ps --status running -q mcp)
restore_mcp=0
finish() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$restore_mcp" -eq 1 ]; then
    if ! docker compose start mcp >/dev/null; then
      printf '%s\n' 'The owner operation ended, but the archive MCP service did not restart. Inspect it locally.' >&2
      exit 1
    fi
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -n "$running" ]; then
  restore_mcp=1
  docker compose stop mcp >/dev/null
fi
docker compose --profile operator run --rm --no-deps operator "$@"
