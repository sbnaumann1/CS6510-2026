#!/usr/bin/env bash
# Trace one request across the microservices and the broker by its X-Request-ID
# (spec 007 FR-020, spec 008 FR-017, SC-011; quickstart step 4).
#
#   ./scripts/trace_request.sh               # shop once through the gateway, then trace it
#   ./scripts/trace_request.sh demo-1        # trace a request you made yourself:
#   curl -H 'X-Request-ID: demo-1' -X POST localhost:8080/transactions/tx-1/complete -d '{}'
#
# Prints, in time order, every service's log line for the id (one per coordination step),
# then every event carrying the id on the completion and stock topics, with its partition and
# offset. The id travels in the event envelope and in the `x-request-id` Kafka header, so a
# reader can follow one request across Gateway, Checkout, Kafka and Inventory.
set -euo pipefail
cd "$(dirname "$0")/.."

GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
ID="${1:-}"

if [ -z "$ID" ]; then
  ID="trace-$(date +%s)"
  echo "No id given: shopping once through the gateway with X-Request-ID $ID"
  tx=$(curl -s -X POST "$GATEWAY/transactions" -H 'content-type: application/json' -d '{"stationId":"trace"}' \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["transactionId"])')
  curl -s -o /dev/null -X POST "$GATEWAY/transactions/$tx/items" -H 'content-type: application/json' -d '{"sku":"SKU-000001"}'
  curl -s -o /dev/null -X POST "$GATEWAY/transactions/$tx/complete" -H 'content-type: application/json' \
       -H "X-Request-ID: $ID" -d '{}'
  sleep 1
fi

echo
echo "== Log lines carrying $ID (every service) =="
docker compose logs --no-color --timestamps catalog checkout inventory analytics 2>/dev/null \
  | grep -F "\"request_id\": \"$ID\"" \
  | sort -k3 \
  | sed -E 's/^([a-z]+)-[0-9]+ +\| ([0-9T:.-]+Z) /\2 \1 /' \
  || { echo "no log lines for request id $ID" >&2; }

echo
echo "== Events carrying $ID on the broker =="
found=0
for topic in checkout.completions.v1 inventory.stock.v1; do
  lines=$(docker compose exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh \
            --bootstrap-server kafka:9092 --topic "$topic" --from-beginning --timeout-ms 5000 \
            --property print.headers=true --property print.partition=true \
            --property print.offset=true --property print.key=true 2>/dev/null \
          | grep -F "x-request-id:$ID" || true)
  if [ -n "$lines" ]; then
    found=1
    echo "$lines" | sed "s/^/$topic  /" | cut -c1-240
  fi
done
[ $found -eq 1 ] || echo "(no events found for $ID: was it a completion?)"
