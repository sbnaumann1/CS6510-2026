#!/usr/bin/env bash
# Run the load-client reports end to end: reseed, start the server, load it,
# verify the invariant, stop the server — once per run.
#
# Usage:
#   ./scripts/run_load_tests.sh [normal|stress|all]     (default: all)
#   STOCK_PER_ITEM=200000 ./scripts/run_load_tests.sh normal   # no stock-outs (001 V7)
#
# The server is restarted after every reseed on purpose: seed.py --reset drops
# the schema, and a running server would keep its cached catalog while every
# write fails.
set -euo pipefail

BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT="$BACKEND/../load-client"
REPORTS="$CLIENT/reports"
MODE="${1:-all}"
SERVER_PID=""
FAILED=0

case "$MODE" in
  normal) RUNS=("normal 10 60") ;;
  stress) RUNS=("stress 100 120") ;;
  all)    RUNS=("normal 10 60" "stress 100 120") ;;
  *) echo "usage: $0 [normal|stress|all]" >&2; exit 2 ;;
esac

port_in_use() { lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; }

stop_server() {
  [ -n "$SERVER_PID" ] || return 0
  kill "$SERVER_PID" 2>/dev/null || true
  for _ in $(seq 1 30); do port_in_use || break; sleep 0.5; done
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
}
trap stop_server EXIT INT TERM

if port_in_use; then
  echo "Port 8080 is already in use — stop that server first." >&2
  exit 1
fi

cd "$BACKEND"
(cd "$CLIENT" && ./build.sh >/dev/null)

for run in "${RUNS[@]}"; do
  read -r name stations duration <<<"$run"
  echo
  echo "=== $name run: $stations stations, ${duration}s ==="

  uv run python scripts/seed.py --reset

  ./scripts/run_server.sh >"$BACKEND/server-$name.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 60); do
    curl -sf -o /dev/null http://localhost:8080/items && break
    sleep 0.5
  done
  curl -sf -o /dev/null http://localhost:8080/items \
    || { echo "Server did not start; see server-$name.log" >&2; exit 1; }

  before=$(ls -t "$REPORTS"/report-*.json 2>/dev/null | head -1 || true)
  (cd "$CLIENT" && ./run.sh --baseUrl=http://localhost:8080 \
      --stations="$stations" --duration="$duration")
  report=$(ls -t "$REPORTS"/report-*.json | head -1)
  [ "$report" != "$before" ] || { echo "No new report was written." >&2; exit 1; }

  stop_server

  if ! uv run python scripts/verify_invariant.py; then
    echo "INVARIANT FAILED for $name run" >&2
    FAILED=1
  fi

  python3 - "$report" "$name" <<'EOF'
import json, sys
path, name = sys.argv[1], sys.argv[2]
d = json.load(open(path))
ops = {o["operation"]: o for o in d["operations"]}
attempted = ops["START_TRANSACTION"]["successCount"]
succeeded = d["totalTransactions"]
failed = ops["COMPLETE_TRANSACTION"]["errorCount"]
pct = lambda n: f"{n / attempted:.1%}" if attempted else "n/a"
print(f"\n--- {name} summary ({path.rsplit('/', 1)[-1]}) ---")
print(f"attempted: {attempted}   succeeded: {succeeded} ({pct(succeeded)})   "
      f"failed: {failed} ({pct(failed)})")
EOF
done

exit "$FAILED"
