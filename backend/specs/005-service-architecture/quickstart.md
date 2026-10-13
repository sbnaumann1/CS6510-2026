# Quickstart & Validation: Service-Based Architecture (005)

All commands run from `backend/` unless noted. Unit names, ports and health checks are in
[contracts/compose-topology.md](./contracts/compose-topology.md). The route table is in
[contracts/gateway-routes.md](./contracts/gateway-routes.md).

## Prerequisites

- Docker Desktop running (`docker info` succeeds), set to **≥ 8 CPUs / ≥ 8 GB** (Settings → Resources).
- Compose v2 (`docker compose version` ≥ 2.20, needed for `--wait`).
- JDK 21+ for the load client. `uv` for the host-side test suite only.
- Nothing else listening on host port 8080 or 5433.

## V1. Bring the system up (FR-005, FR-005a)

```bash
docker compose down -v                    # fresh state
docker compose up -d --build --wait       # returns only when every unit is healthy
docker compose ps                         # postgres, catalog, checkout, inventory, analytics, gateway: healthy; seed: exited (0)
curl -s localhost:8080/items | head -c 120
```

**Expect**: `{"items":[{"sku":"SKU-000001",…` and all six long-running units reported healthy.

## V2. The services, listed (FR-018, US4 scenario 3)

```bash
docker compose ps --services
```

**Expect**: `postgres seed catalog checkout inventory analytics gateway`. The four domain
services are catalog, checkout, inventory and analytics. `gateway` is infrastructure.
`postgres` is the shared database, and `seed` and `tools` are operator tools.

## V3. Contract unchanged through the gateway (SC-001)

```bash
uv sync
uv run pytest -q                                   # in-process: four service apps behind the test dispatcher
docker compose down -v
docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait
GATEWAY_URL=http://localhost:8080 \
TEST_DATABASE_URL=postgresql+asyncpg://checkout:checkout@127.0.0.1:5433/checkout_test \
  uv run pytest -q tests/contract                  # same contract tests, over real HTTP
docker compose -f compose.yaml -f compose.test.yaml down -v
```

**Expect**: both runs are all green, with no test assertions edited relative to spec 004.

## V4. Boundaries hold (FR-009, FR-011, FR-016, SC-004)

```bash
uv run python scripts/check_boundaries.py
```

**Expect**: `0 violations (7 rules, 4 services, 6 tables)`.

## V5. Analytics advances with no channel from Checkout (US3 scenario 2, FR-012, SC-005)

With the stack from V1 up, bypass the gateway and talk to Checkout directly on 8082. Then
read Analytics directly on 8084:

```bash
tx=$(curl -s -XPOST localhost:8082/transactions -H 'content-type: application/json' \
       -d '{"stationId":"qs"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["transactionId"])')
for i in $(seq 1 500); do
  curl -s -o /dev/null -XPOST localhost:8082/transactions/$tx/items \
       -H 'content-type: application/json' -d '{"sku":"SKU-000001"}'
done
sleep 2
curl -s 'localhost:8084/analytics/popular-items?limit=1'
```

**Expect**: `windowEnd` is `500` (the first slide boundary, research R5) and `SKU-000001` is
rank 1 with `scanCount` 500. Nothing in Checkout knows Analytics exists.

Then scan another 1 000 items the same way and confirm the persistent pipeline slid without
being restarted (FR-012a):

```bash
curl -s 'localhost:8084/analytics/popular-items?limit=1'      # windowStart 500, windowEnd 1500
docker compose logs analytics | grep -c 'pipeline started'    # 1: one pipeline for the process lifetime
docker compose logs analytics | grep -c 'window published'    # ≥ 3 (500, 1000, 1500), all from that one pipeline
```

## V6. Independent deployability (US2, SC-003)

```bash
./scripts/check_independence.sh
```

What the script does and asserts:
1. `docker compose stop analytics`.
2. `GET /items` and a full start → scan → complete flow through `:8080` all succeed.
3. `GET /analytics/popular-items` returns `503` with the body `{"error":"INTERNAL_ERROR","message":"analytics service is unavailable."}`.
4. It scans 500 more items, runs `docker compose start analytics`, and waits until it's healthy.
5. Within 2 s, popular-items reflects the scans made during the outage (catch-up, US2 scenario 2).
6. It repeats steps 1–3 for `catalog` and `inventory`. Checkout isn't stopped: it carries the load client's whole flow.

Then, to check that a rebuild stays independent (US2 scenario 3):

```bash
echo "# probe" >> services/analytics/src/analytics_service/api.py   # a real content change:
docker compose up -d --build --wait                                 # Docker caches by content, so `touch` rebuilds nothing
docker compose ps --format '{{.Service}} {{.CreatedAt}}'            # only analytics was recreated
git checkout services/analytics/src/analytics_service/api.py && docker compose up -d --build --wait
```

Verified 2026-10-13: only `analytics` was recreated. Image IDs change on every build
(BuildKit provenance metadata), but compose recreates a container only when its image
content changes.

## V7. The graded load runs (FR-017, SC-002, SC-006)

```bash
./scripts/run_load_tests.sh all
```

For each run, the script does a fresh `down -v` and `up --wait`, then the load client. It runs
`./run.sh` for the default run, then `./run.sh --stations=100 --duration=120` for the stress
run. It moves each report into `load-client/reports/005-service-architecture/`, verifies the
invariant, and prints the summary.

**Expect**:
- Two new JSON reports in that folder, with `config.baseUrl = http://localhost:8080` and stations/duration of 10/60 and 100/120 respectively.
- `INVARIANT PASS` after each run.
- No `INTERNAL_ERROR`, transport errors or timeouts. `INSUFFICIENT_STOCK` 409s at COMPLETE are expected at the default 10 000 units of stock.
- tx/s reported alongside the spec 004 baseline (320.9 default / 269.1 stress). Any gap beyond 20% is explained in the README (research R9).

## V8. Teardown

```bash
docker compose down -v
```
