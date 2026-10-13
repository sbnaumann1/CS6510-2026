#!/usr/bin/env bash
# Independent deployability check (spec US2, SC-003; quickstart V6).
#
# Needs the stack running:  docker compose up -d --build --wait
#
# For each of analytics, catalog, inventory: stop it, prove every other route
# still works through the gateway and the stopped route answers 503 in the
# contract's error shape, restart it, and wait until healthy. For analytics it
# also scans 500 items during the outage and checks the restarted pipeline
# catches up from the shared database. Checkout is not stopped: it carries the
# load client's whole flow.
#
#   ./scripts/check_independence.sh
#   SKIP_CATCHUP=1 ./scripts/check_independence.sh   # before the persistent pipeline exists
set -euo pipefail
cd "$(dirname "$0")/.."

G=http://localhost:8080
SLIDE=500

fail() { echo "FAIL: $*" >&2; exit 1; }

status() {  # <method> <path> [json-body] -> prints HTTP status
  local m=$1 p=$2 b=${3:-}
  if [ -n "$b" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$m" "$G$p" -H 'content-type: application/json' -d "$b"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$m" "$G$p"
  fi
}

json_field() {  # <field> < json; prints nothing if the body lacks it (e.g. a 503)
  python3 -c "import sys, json
try:
    print(json.load(sys.stdin)[sys.argv[1]])
except Exception:
    pass" "$1"
}

start_tx() {
  curl -s -X POST "$G/transactions" -H 'content-type: application/json' \
    -d '{"stationId":"independence"}' | json_field transactionId
}

checkout_flow() {  # start -> scan -> complete must be 201/200/200
  local tx s c
  tx=$(start_tx) || fail "start transaction"
  s=$(status POST "/transactions/$tx/items" '{"sku":"SKU-000001"}')
  c=$(status POST "/transactions/$tx/complete" '{}')
  [ "$s" = 200 ] && [ "$c" = 200 ] || fail "checkout flow: scan=$s complete=$c"
}

max_scan_id() {
  docker compose exec -T postgres psql -U checkout -d "${POSTGRES_DB:-checkout}" -tAc \
    "select coalesce(max(id),0) from transaction_item" | tr -d '[:space:]'
}

wait_healthy() {  # <service>
  local id
  id=$(docker compose ps -q "$1")
  for _ in $(seq 1 60); do
    [ "$(docker inspect --format '{{.State.Health.Status}}' "$id")" = healthy ] && return 0
    sleep 1
  done
  fail "$1 did not become healthy within 60s"
}

expect_down_body() {  # <service> <path>
  local body want
  body=$(curl -s "$G$2")
  want="{\"error\":\"INTERNAL_ERROR\",\"message\":\"$1 service is unavailable.\"}"
  python3 - "$body" "$want" <<'EOF' || fail "$1 outage body: $body"
import json, sys
sys.exit(0 if json.loads(sys.argv[1]) == json.loads(sys.argv[2]) else 1)
EOF
}

route_of() {  # macOS ships bash 3.2: no associative arrays
  case "$1" in
    catalog) echo /items ;;
    inventory) echo /inventory/low-stock ;;
    analytics) echo /analytics/popular-items ;;
  esac
}

for svc in analytics catalog inventory; do
  echo "== $svc"
  docker compose stop "$svc" >/dev/null 2>&1

  # The stopped service's route: 503 in the ApiError shape.
  path=$(route_of "$svc")
  code=$(status GET "$path")
  [ "$code" = 503 ] || fail "$svc route returned $code while stopped (want 503)"
  expect_down_body "$svc" "$path"

  # Every other route keeps working.
  [ "$svc" = catalog ] || [ "$(status GET /items)" = 200 ] || fail "/items while $svc down"
  [ "$svc" = inventory ] || [ "$(status GET /inventory/low-stock)" = 200 ] || fail "low-stock while $svc down"
  [ "$svc" = analytics ] || [ "$(status GET /analytics/popular-items)" = 200 ] || fail "popular while $svc down"
  checkout_flow

  if [ "$svc" = analytics ]; then
    tx=$(start_tx)
    for _ in $(seq 1 $SLIDE); do
      code=$(status POST "/transactions/$tx/items" '{"sku":"SKU-000002"}')
      [ "$code" = 200 ] || fail "scan during analytics outage returned $code"
    done
    echo "   scanned $SLIDE items while analytics was down"
  fi

  docker compose start "$svc" >/dev/null 2>&1
  wait_healthy "$svc"

  if [ "$svc" = analytics ] && [ -z "${SKIP_CATCHUP:-}" ]; then
    want=$(( $(max_scan_id) / SLIDE * SLIDE ))
    got=""
    # The gateway re-resolves upstream addresses every 5 s (nginx `resolver
    # valid=5s`), so it may answer 503 briefly after a restart even though the
    # container is healthy. Allow 15 s for routing to recover plus catch-up.
    for _ in $(seq 1 150); do
      got=$(curl -s "$G/analytics/popular-items" | json_field windowEnd)
      [ "$got" = "$want" ] && break
      sleep 0.1
    done
    [ "$got" = "$want" ] || fail "analytics catch-up: windowEnd=${got:-<no window>}, want $want"
    echo "   caught up: windowEnd=$got"
  fi
  echo "PASS $svc"
done
