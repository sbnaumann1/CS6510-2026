#!/usr/bin/env bash
# Crash recovery under load (spec 007 FR-015, SC-003; quickstart V8).
#
# Needs the stack running:  docker compose up -d --build --wait
#
# Runs shoppers in the background, kills Checkout with `docker kill` (no graceful shutdown)
# while completions are in flight, brings it back, stops the shoppers, waits for the
# recovery loop, and requires the cross-store invariant to hold: no transaction left
# COMPLETING, no completion left mid-saga, stock equal to what was sold.
#
#   ./scripts/check_crash_recovery.sh            # 8 shoppers, kill after 6 s
#   KILLS=3 ./scripts/check_crash_recovery.sh    # kill Checkout three times
set -uo pipefail
cd "$(dirname "$0")/.."

G=http://localhost:8080
WORKERS=${WORKERS:-8}
KILLS=${KILLS:-1}
RECOVERY_WAIT=${RECOVERY_WAIT:-25}   # the lease (10 s) plus the loop interval, with margin

wait_healthy() {
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q "$1")" 2>/dev/null)" = healthy ] && return 0
    sleep 1
  done
  echo "FAIL: $1 did not become healthy" >&2; exit 1
}

shopper() {  # one shopper: buy a few items, complete, repeat until told to stop
  while [ -e "$STOP" ] && [ ! -s "$STOP" ]; do
    tx=$(curl -s -m 5 -X POST "$G/transactions" -H 'content-type: application/json' \
         -d '{"stationId":"crash"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["transactionId"])' 2>/dev/null) || { sleep 0.2; continue; }
    for sku in SKU-000001 SKU-000002 SKU-000003; do
      curl -s -m 5 -o /dev/null -X POST "$G/transactions/$tx/items" -H 'content-type: application/json' -d "{\"sku\":\"$sku\"}"
    done
    curl -s -m 8 -o /dev/null -X POST "$G/transactions/$tx/complete" -H 'content-type: application/json' -d '{}'
  done
}

STOP=$(mktemp); : > "$STOP"          # an empty file means "keep going"
trap 'echo stop > "$STOP"; rm -f "$STOP"' EXIT   # never leave shoppers running
pids=()
for _ in $(seq 1 "$WORKERS"); do shopper & pids+=($!); done
echo "$WORKERS shoppers running"

for k in $(seq 1 "$KILLS"); do
  sleep 6
  echo "kill $k: docker kill checkout"
  docker kill "$(docker compose ps -q checkout)" >/dev/null
  sleep 3
  docker compose up -d --no-deps checkout >/dev/null 2>&1
  wait_healthy checkout
  echo "checkout is back"
done
sleep 4

echo stop > "$STOP"                  # a non-empty file stops the shoppers
wait "${pids[@]}" 2>/dev/null
rm -f "$STOP"

echo "waiting ${RECOVERY_WAIT}s for the recovery loop"
sleep "$RECOVERY_WAIT"

docker compose --profile tools run --rm --build tools
