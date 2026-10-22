# Quickstart: Validating the Microservices Architecture

Run from `backend/`. Prerequisites: Docker Desktop running (≥ 8 CPUs, ≥ 8 GB), JDK 21+, `uv`, and
native PostgreSQL 17 for the host-side tests. Contracts: [contracts/](./contracts/). Data:
[data-model.md](./data-model.md).

## V1. Boundaries hold (no Docker needed)

```bash
uv run python scripts/check_boundaries.py
```

Expect: exit 0, all ten rules in [service-boundaries.md](./contracts/service-boundaries.md) pass.
Covers FR-005, FR-007, FR-008, FR-022, SC-007.

## V2. Contract and integration tests, in-process (four test databases)

```bash
uv run pytest tests/contract tests/integration tests/unit
```

Expect: all pass; contract assertions are unchanged from 005 (SC-001).

## V3. Failure injection at every saga step

```bash
uv run pytest tests/failure
```

Expect: for each of rows A–H in [completion-saga.md](./contracts/completion-saga.md), the final
state matches the table and `verify_invariant` logic reports consistent stores (FR-023, SC-003,
SC-004). Includes: reserve rejected, reserve timeout then success on retry, reserve timeout
then compensation, finalize failure, duplicate and concurrent `complete`, late `reserve` after
`release`, relay and ingest failures.

## V4. Bring up the stack

```bash
docker compose down -v
docker compose up -d --build --wait
docker compose ps
```

Expect: four databases, four one-shot seeds `exited (0)`, four services and the gateway `healthy`.
Then confirm isolation:

```bash
docker compose exec checkout python -c "import socket; socket.create_connection(('inventory-db', 5432), 2)"
```

Expect: fails (name does not resolve). Repeat for each service-to-foreign-database pair
(FR-008).

## V5. Contract tests over real HTTP

```bash
GATEWAY_URL=http://localhost:8080 uv run pytest tests/contract
```

Expect: pass (SC-001).

## V6. One request, traced across services

```bash
curl -s -H 'X-Request-ID: demo-1' -X POST localhost:8080/transactions -d '{"stationId":"s1"}' -H 'content-type: application/json'
# scan an item, then complete using the returned id
docker compose logs checkout inventory | grep demo-1
```

Expect: the completion's id appears in Checkout's and Inventory's log lines, one per saga step
(FR-020, SC-008).

## V7. Isolation under outage

```bash
docker compose stop inventory
# GET /items, POST /transactions, scans: succeed.  POST .../complete and GET /inventory/low-stock: 503 within 5 s
docker compose start inventory
```

Repeat for `catalog`, `analytics`, `checkout`. Expect: only dependent routes fail, each with the
contract's error shape (FR-019, SC-005). After restarting Inventory, the transaction left `OPEN`
completes normally.

## V8. Crash recovery

```bash
# start a completion under load, then:
docker compose kill checkout && docker compose up -d checkout
sleep 15
docker compose run --rm tools python -m verify_invariant
```

Expect: PASS, no `COMPLETING` transactions, no non-terminal completions (FR-015, SC-003).

## V9. Analytics catch-up

```bash
docker compose stop analytics
# run a short load, then
docker compose start analytics
sleep 30
curl -s localhost:8080/analytics/popular-items | head
```

Expect: ranking reflects all scans made while it was down (SC-006).

## V10. Graded load reports

```bash
./scripts/run_load_tests.sh all        # reports to ../load-client/reports/007-microservices-architecture/
```

Runs the default configuration and `--stations=100 --duration=120`, each from `down -v`, each
followed by `verify_invariant`. Expect: zero transport failures, only contract-defined business
errors, invariant PASS (SC-002). Compare tx/s with 005's stress run and record the ratio and, if
below 70 %, the measured cause (SC-009).
