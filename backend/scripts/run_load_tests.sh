#!/usr/bin/env bash
# Run the graded load-client reports end to end against the compose stack
# (spec 007 FR-025; spec 005 FR-017, research R14): fresh stack, load it, verify the invariant —
# once per run.
#
# Usage:
#   ./scripts/run_load_tests.sh [default|stress|all]    (default: all; `normal` = default)
#   STOCK_PER_ITEM=200000 ./scripts/run_load_tests.sh default   # no stock-outs (001 V7)
#   REPORT_DIR=other-name ./scripts/run_load_tests.sh all   # reports subdirectory (default 007-microservices-architecture)
#
# Every run starts from `docker compose down -v`, so the database is seeded
# fresh. The load client runs on the host with exactly the assignment's flags:
#   default:  ./run.sh
#   stress:   ./run.sh --stations=100 --duration=120
# Each report is moved into load-client/reports/007-microservices-architecture/, and the
# stack is left running after the last run for the pre-push review.
set -euo pipefail

BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT="$BACKEND/../load-client"
REPORTS="$CLIENT/reports"
OUT="$REPORTS/${REPORT_DIR:-007-microservices-architecture}"
MODE="${1:-all}"
FAILED=0

case "$MODE" in
  default|normal) RUNS=("default") ;;
  stress)         RUNS=("stress") ;;
  all)            RUNS=("default" "stress") ;;
  *) echo "usage: $0 [default|stress|all]" >&2; exit 2 ;;
esac

cd "$BACKEND"
mkdir -p "$OUT" logs
docker info >/dev/null 2>&1 || { echo "Docker is not running — start Docker Desktop." >&2; exit 1; }
(cd "$CLIENT" && ./build.sh >/dev/null)

for name in "${RUNS[@]}"; do
  echo
  echo "=== $name run ==="

  docker compose down -v >/dev/null 2>&1
  docker compose up -d --build --wait

  before=$(ls -t "$REPORTS"/report-*.json 2>/dev/null | head -1 || true)
  if [ "$name" = stress ]; then
    (cd "$CLIENT" && ./run.sh --stations=100 --duration=120)
  else
    (cd "$CLIENT" && ./run.sh)
  fi
  report=$(ls -t "$REPORTS"/report-*.json | head -1)
  [ "$report" != "$before" ] || { echo "No new report was written." >&2; exit 1; }
  mv "$report" "$OUT/"
  report="$OUT/$(basename "$report")"

  docker compose logs --no-color > "logs/compose-$name.log"

  # The cross-store check: each service reports on its own data; no database is opened.
  if ! docker compose --profile tools run --rm --build tools; then
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
print(f"tx/s: {d['transactionsPerSecond']:.1f}   items/s: {d['itemsPerSecond']:.0f}")
EOF
done

echo
echo "Stack left up for review; tear down with: docker compose down -v"
exit "$FAILED"

# ---------------------------------------------------------------------------
# Manual equivalent, run from backend/:
#
#   # Default run: 10 stations, 60 s
#   docker compose down -v && docker compose up -d --build --wait
#   cd ../load-client && ./build.sh && ./run.sh
#   mv reports/report-*.json reports/007-microservices-architecture/
#   cd ../backend && docker compose --profile tools run --rm --build tools
#
#   # Stress run: 100 stations, 120 s
#   docker compose down -v && docker compose up -d --build --wait
#   cd ../load-client && ./run.sh --stations=100 --duration=120
#   mv reports/report-*.json reports/007-microservices-architecture/
#   cd ../backend && docker compose --profile tools run --rm --build tools
#
# Optional no-stock-out variant (001 V7): prefix the up step with STOCK_PER_ITEM=200000
# ---------------------------------------------------------------------------
