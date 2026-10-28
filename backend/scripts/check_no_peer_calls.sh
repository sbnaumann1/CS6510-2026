#!/usr/bin/env bash
# No service-to-service calls at runtime (spec 008 FR-002, SC-002; quickstart step 3).
#
# Needs the stack running:  docker compose up -d --build --wait
#
# While a full shopping workload runs through the gateway, every microservice container
# samples its own TCP connections. The check passes when:
#   - the workload itself succeeds (every public endpoint works), and
#   - no service ever held a connection to another service's IP address
# (Connections to the broker, the gateway and the service's own database are expected.)
# The topology already makes a peer connection impossible (scripts/check_isolation.sh); this
# proves nothing even tried.
set -uo pipefail
cd "$(dirname "$0")/.."

GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
SERVICES="catalog checkout inventory analytics"
SECONDS_TO_SAMPLE="${SAMPLE_SECONDS:-12}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

# The container IPs of every microservice (across all of its networks), one per line.
service_ips() {
  for svc in $SERVICES; do
    cid="$(docker compose ps -q "$svc")"
    docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$cid" | tr ' ' '\n' \
      | grep -v '^$' | sed "s/^/$svc /"
  done
}

# Runs inside a container: print each remote IPv4 address seen in /proc/net/tcp for N seconds.
SAMPLER='
import socket, struct, sys, time
seen = set()
end = time.time() + float(sys.argv[1])
while time.time() < end:
    with open("/proc/net/tcp") as f:
        next(f)
        for line in f:
            remote = line.split()[2].split(":")[0]
            ip = socket.inet_ntoa(struct.pack("<I", int(remote, 16)))
            if ip != "0.0.0.0" and not ip.startswith("127."):
                seen.add(ip)
    time.sleep(0.05)
print("\n".join(sorted(seen)))
'

echo "Sampling connections in every service for ${SECONDS_TO_SAMPLE}s while a workload runs..."
for svc in $SERVICES; do
  docker compose exec -T "$svc" python -c "$SAMPLER" "$SECONDS_TO_SAMPLE" > "$OUT/$svc.ips" 2>/dev/null &
done

FAILED=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1" >&2; FAILED=1; }

# The workload: shop through the gateway, touching every service.
fail_req=0
req() {  # <method> <path> [json]
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" "$GATEWAY$2" -H 'content-type: application/json' ${3:+-d "$3"})
  case "$code" in 2*) ;; *) fail_req=$((fail_req + 1)); echo "  request $1 $2 -> $code" >&2 ;; esac
}
end=$(( $(date +%s) + SECONDS_TO_SAMPLE - 3 ))
n=0
while [ "$(date +%s)" -lt "$end" ]; do
  tx=$(curl -s -X POST "$GATEWAY/transactions" -H 'content-type: application/json' -d '{"stationId":"peer-check"}' \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["transactionId"])')
  for sku in SKU-000001 SKU-000002 SKU-000003; do req POST "/transactions/$tx/items" "{\"sku\":\"$sku\"}"; done
  req POST "/transactions/$tx/complete" '{}'
  req GET "/transactions/$tx"
  req GET "/items"
  req GET "/inventory/low-stock"
  req GET "/analytics/popular-items"
  n=$((n + 1))
done
wait

echo "Workload: $n shopping sessions through the gateway"
[ "$n" -gt 0 ] && [ "$fail_req" -eq 0 ] && ok "every public endpoint answered 2xx" || bad "$fail_req request(s) failed"

service_ips > "$OUT/service_ips"
for svc in $SERVICES; do
  # IPs of the other services, excluding this service's own addresses.
  peers=$(awk -v me="$svc" '$1 != me {print $2}' "$OUT/service_ips")
  hit=""
  for ip in $peers; do
    grep -qx "$ip" "$OUT/$svc.ips" && hit="$hit $ip"
  done
  if [ -n "$hit" ]; then bad "$svc held a connection to another service:$hit"; else ok "$svc never connected to another service"; fi
done

[ $FAILED -eq 0 ] && echo "no peer calls: PASS" || { echo "no peer calls: FAIL" >&2; exit 1; }
