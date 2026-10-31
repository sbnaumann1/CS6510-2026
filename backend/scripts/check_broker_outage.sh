#!/usr/bin/env bash
# Broker outage under load (spec 008 FR-012, FR-014, FR-016, US3 scenario 4, SC-007).
#
# Needs the stack running:  docker compose up -d --build --wait
#
# Runs shoppers in the background, stops Kafka while completions are in flight, keeps it down
# for a while, brings it back, stops the shoppers, waits for the outbox sweepers and the
# recovery loop, and requires:
#   - no transaction left COMPLETING, no completion left mid-saga (the cross-store invariant)
#   - no outbox row left unpublished (nothing lost between a state change and its event)
#   - Analytics has read every scan
#   - the dead-letter topic is empty (an outage is not a poison event)
#
#   ./scripts/check_broker_outage.sh                 # 8 shoppers, broker down for 15 s
#   OUTAGE=30 ./scripts/check_broker_outage.sh
set -uo pipefail
cd "$(dirname "$0")/.."

G=http://localhost:8080
WORKERS=${WORKERS:-8}
OUTAGE=${OUTAGE:-15}
RECOVERY_WAIT=${RECOVERY_WAIT:-25}   # the recovery lease (10 s) plus the loop interval, with margin

wait_healthy() {
  for _ in $(seq 1 90); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q "$1")" 2>/dev/null)" = healthy ] && return 0
    sleep 1
  done
  echo "FAIL: $1 did not become healthy" >&2; exit 1
}

shopper() {  # one shopper: buy a few items, complete, repeat until told to stop
  while [ -e "$STOP" ] && [ ! -s "$STOP" ]; do
    tx=$(curl -s -m 5 -X POST "$G/transactions" -H 'content-type: application/json' \
         -d '{"stationId":"outage"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["transactionId"])' 2>/dev/null) || { sleep 0.2; continue; }
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

sleep 6
echo "docker compose stop kafka  (down for ${OUTAGE}s)"
docker compose stop kafka >/dev/null 2>&1
sleep "$OUTAGE"
echo "docker compose start kafka"
docker compose start kafka >/dev/null 2>&1
wait_healthy kafka
echo "kafka is back"
sleep 8

echo stop > "$STOP"                  # a non-empty file stops the shoppers
wait "${pids[@]}" 2>/dev/null
rm -f "$STOP"

echo "waiting ${RECOVERY_WAIT}s for the outbox sweepers and the recovery loop"
sleep "$RECOVERY_WAIT"

FAILED=0
echo "Dead letters:"
dead=$(docker compose exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh \
         --bootstrap-server kafka:9092 --topic dead-letters.v1 --from-beginning --timeout-ms 5000 2>/dev/null | grep -c '^{' || true)
if [ "${dead:-0}" -eq 0 ]; then echo "  ok    the dead-letter topic is empty"; else echo "  FAIL  $dead dead letter(s): an outage must not dead-letter" >&2; FAILED=1; fi

echo "Cross-store invariant (stock, completions, outbox, scans):"
docker compose --profile tools run --rm --build tools || FAILED=1

[ $FAILED -eq 0 ] && echo "broker outage: PASS" || { echo "broker outage: FAIL" >&2; exit 1; }
