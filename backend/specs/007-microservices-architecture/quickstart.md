# Quickstart: Validating the Microservices Architecture

Run from `backend/`. Prerequisites: Docker Desktop running (≥ 8 CPUs, ≥ 8 GB), JDK 21+, `uv`, and
native PostgreSQL 17 for the host-side tests. Contracts: [contracts/](./contracts/). Data:
[data-model.md](./data-model.md).

## V1. Boundaries hold (no Docker needed)

```bash
uv run python scripts/check_boundaries.py
```

Expect: exit 0, all eleven rules in [service-boundaries.md](./contracts/service-boundaries.md) pass.
Covers FR-005, FR-007, FR-008, FR-022, SC-007.

## V2. Contract and integration tests, in-process (two test databases and a temporary catalog file)

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
`release`, and Analytics' scan reads failing.

## V4. Bring up the stack, and prove the stores are private

```bash
docker compose down -v
docker compose up -d --build --wait
docker compose ps
./scripts/check_isolation.sh
```

Expect: two databases, three one-shot seeds `exited (0)`, four services and the gateway
`healthy`. `check_isolation.sh` then proves, from inside the running containers, that no service
can open another's database, that only Catalog sees (and can only read) the catalog file, and
that Analytics has no `DATABASE_URL` and ships no database library (FR-007, FR-008, SC-007).

## V5. Contract tests over real HTTP

```bash
docker compose down -v
docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait
GATEWAY_URL=http://localhost:8080 uv run pytest tests/contract
docker compose -f compose.yaml -f compose.test.yaml down -v
```

Expect: pass (SC-001). This uses the small-catalog test stack (its own project, images and
volumes) and resets its databases between tests through their published debug ports.

## V6. One request, traced across services

```bash
curl -s -X POST localhost:8080/transactions -H 'content-type: application/json' -d '{"stationId":"s1"}'
# scan an item, then:
curl -s -X POST localhost:8080/transactions/tx-1/complete -H 'X-Request-ID: demo-1' -d '{}'
./scripts/trace_request.sh demo-1
```

Expect: the id appears in Checkout's and Inventory's log lines, one per saga step, in time order
(FR-020, SC-008).

## V7. Isolation under outage, and independent redeploys

```bash
./scripts/check_independence.sh
```

Expect `independence: PASS`. Part 1 recreates each service alone and proves the other three were
not restarted. Part 2 stops each service in turn and checks which routes fail (only its
dependants, each with the contract's error shape and within 5 s) and that the system heals: a
completion refused while Inventory was down succeeds once it is back (FR-004, FR-006, FR-019,
SC-005).

## V8. Crash recovery

```bash
KILLS=2 ./scripts/check_crash_recovery.sh
```

Runs shoppers, kills Checkout with `docker kill` twice mid-completion, restarts it, waits for the
recovery loop, then runs the cross-store check. Expect: `PASS`, no `COMPLETING` transactions, no
unfinished completions (FR-015, SC-003).

## V9. Analytics catch-up

`check_independence.sh` also covers this (500 scans made while Analytics is down; the ranking
catches up within 30 s of its return, SC-006). By hand:

```bash
docker compose stop analytics
# run a short load, then
docker compose start analytics
curl -s 'localhost:8080/analytics/popular-items?limit=3'
```

Expect: the ranking reflects all scans made while it was down.

Then the derived-state edge case: stop `checkout`, restart `analytics`, and confirm
`/analytics/popular-items` answers `200` with the empty window (no error). Start `checkout` again
and confirm the ranking returns within seconds. Finally stop `checkout` alone and confirm Analytics
keeps serving its last ranking.

## V10. Graded load reports

```bash
./scripts/run_load_tests.sh all        # reports to ../load-client/reports/007-microservices-architecture/
```

Runs the default configuration and `--stations=100 --duration=120`, each from `down -v`, each
followed by the cross-store check (`docker compose --profile tools run --rm --build tools`). Expect: zero transport failures, only contract-defined business
errors, invariant PASS (SC-002). Compare tx/s with 005's stress run and record the ratio and, if
below 70 %, the measured cause (SC-009).
