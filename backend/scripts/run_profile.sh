#!/usr/bin/env bash
# Profile the completion path and try the cheap tuning knobs (spec 009 research).
#
# Usage:
#   ./scripts/run_profile.sh                       # every config, default + stress load
#   ./scripts/run_profile.sh baseline lowlinger    # only the named configs
#   DEFAULT_SECONDS=30 STRESS_SECONDS=60 ./scripts/run_profile.sh
#
# For each config: fresh stack (down -v, up --build, TRACE_TIMING=1), then the unmodified Java
# load client twice (default: 10 stations; stress: 100 stations), then
# scripts/profile_completion.py on the service logs. Reports and breakdowns land in
# logs/profile/. Stock is raised (STOCK_PER_ITEM) so no basket is refused and every completion
# takes the full reserve -> finalize path.
#
# Configs (each changes only what it names, relative to baseline = today's defaults):
#   baseline    nothing
#   lowlinger   producer linger_ms 2 -> 0                                        (step 2)
#   acks1       producer acks all -> 1 (single broker: no durability lost to replicas)  (step 2)
#   fetch       consumer fetch_max_wait_ms 500 -> 20                             (step 2)
#   invscale    Inventory 2 workers x 6 handlers x pool 8 -> 4 x 12 x 12          (step 4)
#   combined    lowlinger + invscale
#   pool10      Checkout pool 16 -> 10 per worker (spec 009 (a): isolates the pool change)
set -euo pipefail

BACKEND="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT="$BACKEND/../load-client"
OUT="$BACKEND/logs/profile"
DEFAULT_SECONDS="${DEFAULT_SECONDS:-60}"
STRESS_SECONDS="${STRESS_SECONDS:-60}"
ALL=(baseline lowlinger acks1 fetch invscale combined pool10)
CONFIGS=("$@"); [ ${#CONFIGS[@]} -gt 0 ] || CONFIGS=("${ALL[@]}")

docker info >/dev/null 2>&1 || { echo "Docker is not running." >&2; exit 1; }
mkdir -p "$OUT"
cd "$BACKEND"
(cd "$CLIENT" && ./build.sh >/dev/null)

set_config() {  # exports the env for one config
  unset KAFKA_LINGER_MS KAFKA_ACKS KAFKA_FETCH_MAX_WAIT_MS INVENTORY_WORKERS \
        INVENTORY_CONCURRENCY INVENTORY_DB_POOL_SIZE CHECKOUT_DB_POOL_SIZE
  case "$1" in
    baseline) ;;
    lowlinger) export KAFKA_LINGER_MS=0 ;;
    acks1) export KAFKA_ACKS=1 ;;
    fetch) export KAFKA_FETCH_MAX_WAIT_MS=20 ;;
    invscale) export INVENTORY_WORKERS=4 INVENTORY_CONCURRENCY=12 INVENTORY_DB_POOL_SIZE=12 ;;
    pool10) export CHECKOUT_DB_POOL_SIZE=10 ;;
    combined) export KAFKA_LINGER_MS=0 INVENTORY_WORKERS=4 INVENTORY_CONCURRENCY=12 INVENTORY_DB_POOL_SIZE=12 ;;
    *) echo "unknown config: $1" >&2; exit 2 ;;
  esac
}

for cfg in "${CONFIGS[@]}"; do
  set_config "$cfg"
  for mode in ${MODES:-default stress}; do
    echo; echo "=== $cfg / $mode ==="
    docker compose down -v >/dev/null 2>&1
    TRACE_TIMING=1 STOCK_PER_ITEM="${STOCK_PER_ITEM:-2000000}" docker compose up -d --build --wait >/dev/null
    before=$(ls -t "$CLIENT"/reports/report-*.json 2>/dev/null | head -1 || true)
    stop="$OUT/.stop-$$"; rm -f "$stop"
    ./scripts/sample_stats.sh "$OUT/$cfg-$mode.stats.csv" "$stop" 900 &
    sampler=$!
    if [ "$mode" = stress ]; then
      (cd "$CLIENT" && ./run.sh --stations=100 --duration="$STRESS_SECONDS") >/dev/null
    else
      (cd "$CLIENT" && ./run.sh --duration="$DEFAULT_SECONDS") >/dev/null
    fi
    touch "$stop"; wait "$sampler" 2>/dev/null || true; rm -f "$stop"
    report=$(ls -t "$CLIENT"/reports/report-*.json | head -1)
    [ "$report" != "$before" ] || { echo "no new report" >&2; exit 1; }
    mv "$report" "$OUT/$cfg-$mode.json"
    docker compose logs --no-color > "$OUT/$cfg-$mode.log"
    uv run python scripts/profile_completion.py "$OUT/$cfg-$mode.log" > "$OUT/$cfg-$mode.txt" || true
    uv run python scripts/summarize_diagnostics.py "$OUT/$cfg-$mode" > "$OUT/$cfg-$mode.diag.txt" || true
    echo "  -> $OUT/$cfg-$mode.{json,log,txt}"
  done
done
docker compose down -v >/dev/null 2>&1
echo; uv run python scripts/profile_summary.py "$OUT"
