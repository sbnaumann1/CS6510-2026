#!/usr/bin/env bash
# Independent deployability and failure containment (spec 007 US2, US4; FR-004, FR-006,
# FR-019; SC-005, SC-006; quickstart V7 and V9).
#
# Needs the stack running:  docker compose up -d --build --wait
#
# Part 1  For each service: recreate it alone while the others keep running, and prove the
#         other three were neither restarted nor made unhealthy.
# Part 2  For each service: stop it and prove that exactly the routes that depend on it fail,
#         each with the contract's 503 error shape, and that the rest keep working. Then
#         restart it and prove the system heals (a refused completion succeeds; Analytics
#         catches up on scans made while it was down).
#
#   ./scripts/check_independence.sh
#   SKIP_CATCHUP=1 ./scripts/check_independence.sh     # skip the 500-scan Analytics drill
set -euo pipefail
cd "$(dirname "$0")/.."

G=http://localhost:8080
SLIDE=500
SERVICES=(catalog checkout inventory analytics)
FAILED=0

pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; FAILED=1; }
expect() {  # <description> <expected> <actual>
  if [ "$2" = "$3" ]; then pass "$1 ($3)"; else fail "$1: expected $2, got $3"; fi
}

status() {  # <method> <path> [json-body] -> prints the HTTP status
  local m=$1 p=$2 b=${3:-}
  if [ -n "$b" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$m" "$G$p" -H 'content-type: application/json' -d "$b"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$m" "$G$p"
  fi
}

body() { curl -s -X "$1" "$G$2" ${3:+-H 'content-type: application/json' -d "$3"}; }

json() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }

new_tx()  { body POST /transactions '{"stationId":"drill"}' | json '["transactionId"]'; }
scan()    { status POST "/transactions/$1/items" "{\"sku\":\"${2:-SKU-000001}\"}"; }

started_at() { docker inspect -f '{{.State.StartedAt}}' "$(docker compose ps -q "$1")"; }
health()     { docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q "$1")"; }

wait_healthy() {  # <service>
  for _ in $(seq 1 60); do
    [ "$(health "$1" 2>/dev/null)" = healthy ] && return 0
    sleep 1
  done
  fail "$1 did not become healthy"; return 1
}

error_shape() {  # stdin: a response body; ok if it is the contract's {"error","message"}
  python3 -c "
import sys, json
d = json.load(sys.stdin)
sys.exit(0 if set(d) == {'error', 'message'} else 1)"
}

# --- precondition: a fresh stack ---------------------------------------------------------
# The drills buy SKU-000001 and expect stock to be there, and expect an empty low-stock list
# (otherwise that route needs Catalog for names, and rightly fails while Catalog is down).
alerts=$(curl -s "$G/inventory/low-stock" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["alerts"]))' 2>/dev/null || echo "?")
if [ "$alerts" != "0" ]; then
  echo "This drill needs a fresh stack (found $alerts low-stock alerts, or the stack is down)." >&2
  echo "Run:  docker compose down -v && docker compose up -d --build --wait" >&2
  exit 2
fi

# --- warm up: Checkout's catalog snapshot, so scans survive a Catalog outage ----------
TX=$(new_tx); scan "$TX" >/dev/null; body POST "/transactions/$TX/complete" '{}' >/dev/null

echo "== Part 1: recreate one service alone"
for svc in "${SERVICES[@]}"; do
  others=(); for s in "${SERVICES[@]}"; do [ "$s" != "$svc" ] && others+=("$s"); done
  for o in "${others[@]}"; do printf -v "before_$o" '%s' "$(started_at "$o")"; done  # bash 3.2: no maps
  docker compose up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1
  wait_healthy "$svc" || true
  for o in "${others[@]}"; do
    was="before_$o"
    if [ "$(started_at "$o")" = "${!was}" ] && [ "$(health "$o")" = healthy ]; then
      pass "$o untouched while $svc was recreated"
    else
      fail "$o was restarted or unhealthy while $svc was recreated"
    fi
  done
done

echo "== Part 2: stop one service at a time"

# Part 1 recreated every service. Checkout and Inventory load their catalog snapshot at
# startup (each worker process on its own), so give that a moment: the Catalog outage below
# tests what it means to (a warm snapshot keeps scans working).
sleep 3

# -- catalog: the snapshots are warm, so nothing but /items itself fails
echo " catalog down"
docker compose stop catalog >/dev/null 2>&1
expect "GET /items"                        503 "$(status GET /items)"
body GET /items | error_shape && pass "/items failure has the contract's error shape" || fail "/items failure shape"
TX=$(new_tx)
expect "start a transaction"               201 "$(status POST /transactions '{"stationId":"drill"}')"
expect "scan (catalog snapshot is warm)"   200 "$(scan "$TX")"
expect "complete"                          200 "$(status POST "/transactions/$TX/complete" '{}')"
expect "GET /inventory/low-stock"          200 "$(status GET /inventory/low-stock)"
expect "GET /analytics/popular-items"      200 "$(status GET /analytics/popular-items)"
docker compose start catalog >/dev/null 2>&1; wait_healthy catalog || true

# -- inventory: completion is refused cleanly and can be retried
echo " inventory down"
TX=$(new_tx); scan "$TX" >/dev/null; scan "$TX" >/dev/null
docker compose stop inventory >/dev/null 2>&1
expect "GET /items"                        200 "$(status GET /items)"
expect "scan"                              200 "$(scan "$TX")"
t0=$(date +%s)
code=$(status POST "/transactions/$TX/complete" '{}')
elapsed=$(( $(date +%s) - t0 ))
expect "complete is refused"               503 "$code"
[ "$elapsed" -le 5 ] && pass "refusal came within 5 s (${elapsed}s)" || fail "refusal took ${elapsed}s"
body POST "/transactions/$TX/complete" '{}' | error_shape && pass "refusal has the contract's error shape" || fail "refusal shape"
expect "the transaction is still open"     OPEN "$(body GET "/transactions/$TX" | json '["status"]')"
expect "GET /inventory/low-stock"          503 "$(status GET /inventory/low-stock)"
expect "GET /analytics/popular-items"      200 "$(status GET /analytics/popular-items)"
docker compose start inventory >/dev/null 2>&1; wait_healthy inventory || true
expect "the same transaction now completes" 200 "$(status POST "/transactions/$TX/complete" '{}')"

# -- analytics: nobody depends on it during scans or completion
echo " analytics down"
docker compose stop analytics >/dev/null 2>&1
TX=$(new_tx)
expect "start a transaction"               201 "$(status POST /transactions '{"stationId":"drill"}')"
expect "scan"                              200 "$(scan "$TX")"
expect "complete"                          200 "$(status POST "/transactions/$TX/complete" '{}')"
expect "GET /analytics/popular-items"      503 "$(status GET /analytics/popular-items)"
if [ -z "${SKIP_CATCHUP:-}" ]; then
  max_before=$(curl -s localhost:8082/internal/reports/scan-count | json '["maxScanId"]')
  for _ in $(seq 1 25); do
    t=$(new_tx); for _ in $(seq 1 20); do scan "$t" SKU-000002 >/dev/null; done
  done
  max_after=$(curl -s localhost:8082/internal/reports/scan-count | json '["maxScanId"]')
  docker compose start analytics >/dev/null 2>&1; wait_healthy analytics || true
  target=$(( max_after - max_after % SLIDE ))
  ok=0
  for _ in $(seq 1 30); do
    end=$(body GET /analytics/popular-items | json '["windowEnd"]')
    [ "$end" -ge "$target" ] && { ok=1; break; }
    sleep 1
  done
  [ $ok -eq 1 ] && pass "popular-items caught up to scan $target within 30 s ($((max_after - max_before)) scans made while down)" \
                || fail "popular-items stuck at ${end:-?}, wanted at least $target"
else
  docker compose start analytics >/dev/null 2>&1; wait_healthy analytics || true
fi

# -- checkout: its own routes fail; Analytics serves what it has, and rebuilds from Checkout
echo " checkout down"
docker compose stop checkout >/dev/null 2>&1
expect "GET /items"                        200 "$(status GET /items)"
expect "GET /inventory/low-stock"          200 "$(status GET /inventory/low-stock)"
expect "GET /analytics/popular-items (serves its last ranking)" 200 "$(status GET /analytics/popular-items)"
expect "POST /transactions"                503 "$(status POST /transactions '{"stationId":"drill"}')"
echo " checkout down, analytics restarted meanwhile"
docker compose restart analytics >/dev/null 2>&1; wait_healthy analytics || true
end=$(body GET /analytics/popular-items | json '["windowEnd"]')
expect "Analytics answers with the empty window, not an error" 0 "$end"
docker compose start checkout >/dev/null 2>&1; wait_healthy checkout || true
ok=0
for _ in $(seq 1 30); do
  end=$(body GET /analytics/popular-items | json '["windowEnd"]')
  [ "$end" -gt 0 ] && { ok=1; break; }
  sleep 1
done
[ $ok -eq 1 ] && pass "Analytics rebuilt its window once Checkout was back (windowEnd $end)" \
              || fail "Analytics did not recover after Checkout returned"

[ $FAILED -eq 0 ] && echo "independence: PASS" || { echo "independence: FAIL" >&2; exit 1; }
