#!/usr/bin/env bash
# Trace one request across the microservices by its X-Request-ID (spec 007 FR-020, SC-008;
# quickstart V6).
#
#   curl -H 'X-Request-ID: demo-1' -X POST localhost:8080/transactions/tx-1/complete -d '{}'
#   ./scripts/trace_request.sh demo-1
#
# Greps every service's log for the id and prints the matching lines, one per saga step or
# outbound call, in time order (docker compose logs prefixes each line with its service).
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -eq 1 ] || { echo "usage: $0 <request-id>" >&2; exit 2; }

docker compose logs --no-color --timestamps catalog checkout inventory analytics 2>/dev/null \
  | grep -F "\"request_id\": \"$1\"" \
  | sort -k3 \
  | sed -E 's/^([a-z]+)-[0-9]+ +\| ([0-9T:.-]+Z) /\2 \1 /' \
  || { echo "no log lines for request id $1" >&2; exit 1; }
