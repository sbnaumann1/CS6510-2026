#!/usr/bin/env bash
# Hot-SKU contention test (spec 008): does Inventory's FOR UPDATE on a hot SKU still drive
# completion latency in the event-driven flow?
#
# Usage:
#   ./scripts/run_hot_sku_test.sh                 # baseline, skewed and extreme, 50 stations, 30 s each
#   STATIONS=100 DURATION=60 ./scripts/run_hot_sku_test.sh
#
# One fresh stack with enough stock that nothing sells out (a 409 would hide the effect),
# then three runs of scripts/hot_sku_load.py against it:
#   baseline  hot-fraction 0.0  every scan uniform over the catalog: no deliberate contention
#   skewed    hot-fraction 0.5  half of all scans hit one SKU
#   extreme   hot-fraction 1.0  every basket holds that SKU
# Compare the `complete` percentiles, the lock-wait samples, and the Inventory `reserve`
# step timings printed at the end (from the services' own structured logs).
set -euo pipefail

BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACKEND"
STATIONS="${STATIONS:-50}"
DURATION="${DURATION:-30}"

docker info >/dev/null 2>&1 || { echo "Docker is not running — start Docker Desktop." >&2; exit 1; }

docker compose down -v >/dev/null 2>&1
STOCK_PER_ITEM="${STOCK_PER_ITEM:-5000000}" docker compose up -d --build --wait

run() {
  echo; echo "=== $1 ==="
  uv run python scripts/hot_sku_load.py --hot-fraction "$2" --stations "$STATIONS" --duration "$DURATION"
}
run baseline 0.0
run skewed   0.5
run extreme  1.0

echo; echo "=== service step timings (all three runs together) ==="
docker compose logs --no-color > logs/compose-hot-sku.log
uv run python scripts/step_timings.py logs/compose-hot-sku.log | grep -E 'inventory|checkout +(complete|reply|wait)' || true
echo
echo "Verifying the invariant (stock, completions, outbox, scans)..."
docker compose --profile tools run --rm tools
