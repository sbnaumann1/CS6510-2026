# Feature Specification: Hot-Path Latency Reduction for the Service Architecture

> **Evidence branch.** The load/profiling harness (`harness/`) and the raw run results
> (`results/`) mentioned in this document are not in this branch's tree. They are kept on
> branch `worktree-perf-hot-path-latency` (commit `6987174`), under
> `backend/specs/006-service-perf-investigation/`. To use them, run
> `git restore --source=worktree-perf-hot-path-latency -- backend/specs/006-service-perf-investigation/harness backend/specs/006-service-perf-investigation/results`
> and add `.perf-runs/` (the harness's output directory) to `backend/.gitignore` and
> `backend/.dockerignore`.

**Feature Branch**: `006-hot-path-latency`

**Created**: 2026-10-16

**Status**: Draft

**Input**: Act on the findings in [investigation.md](./investigation.md).

- **The regression is deployment, not code.** The 005 refactor's throughput loss
  (stress: 269 → 191 tx/s) comes from deploying under Docker Desktop. Natively the 005 code
  matches the pre-005 monolith (827 vs 818 tx/s at 100 stations).
- **Root cause.** The completion holds the hot-SKU stock lock (`SKU-000001`, in 67.7 % of
  baskets) across 3–4 client round trips. Throughput is therefore inversely proportional to
  per-round-trip latency, and containerization raised that latency.
- **Per-request cost is the Python stack, not the database.** A 0.73 ms scan spends 0.05 ms in
  Postgres plus one round trip, 0.06 ms on explicit `BEGIN`/`COMMIT`, 0.14 ms in SQLAlchemy,
  and ~0.48 ms in the HTTP stack.
- **Goal.** Reduce `COMPLETE` latency and per-request overhead without changing the HTTP
  contract, the error catalog or the graded invariant.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Shorten the completion's lock-holding critical section (Priority: P1)

**Actors**: Checkout Service, concurrent station workers, PostgreSQL row locks

`COMPLETE_TRANSACTION` must hold the stock row locks for as few client round trips as
possible. Ideally that is only the `COMMIT` after the locks are granted. The lock, the stock
check, the decrement, the alert insert and the finalize run inside the database in one
statement.

**Why this priority**: The hot-SKU lock serializes completions and, through the closed-loop
client, sets system throughput. The prototype in `harness/proto/` measured:

| Deployment | Throughput | `COMPLETE` p95 |
|---|---|---|
| native | +13 % tx/s | 124 → 30 ms |
| compose | +31 % tx/s | 432 → 172 ms |
| graded stress config | items/s +19 % | 249 → 73 ms |

It is the largest single lever found, and the only one that also helps natively.

**Independent Test**: `harness/run_exp.sh` with the new code against the same baseline
(100 stations / 30 s / stock 2 000 000), plus the graded stress run. Then
`verify_invariant` and `pg_stat_database.deadlocks`.

**Acceptance Scenarios**:

1. **Given** the compose stack at 100 stations with ample stock, **When** the change is
   deployed, **Then** `COMPLETE` p95 is ≤ 200 ms (baseline 432 ms) and tx/s is ≥ 500
   (baseline 409).
2. **Given** the graded stress config, **When** it is run, **Then** `COMPLETE` p95 is
   ≤ 100 ms (baseline 249 ms), the invariant holds, and the only errors are
   `409 INSUFFICIENT_STOCK`.
3. **Given** concurrent completions on overlapping hot SKUs, **When** they run, **Then**
   there are zero deadlocks, stock never goes negative, and each completion is all-or-nothing.
4. **Given** a completion that crosses the low-stock threshold, **When** it commits, **Then**
   exactly one alert row is inserted, in the same transaction as the decrement (SC-005 of 001).

---

### User Story 2 — Cut per-request round trips on single-statement paths (Priority: P2)

**Actors**: Checkout Service (scan, start)

Scan and start each run one atomic statement, yet pay three round trips
(`BEGIN`, statement, `COMMIT`). They should run in autocommit, as one round trip.

**Why this priority**: Scans are ~90 % of requests. Natively this saves ~0.06 ms of a 0.73 ms
scan, and more where each round trip costs more (inside the Docker VM). It also takes load
off the saturated checkout workers.

**Independent Test**: `pg_stat_statements` shows no `BEGIN` for scan or start requests.
Compare scan p50/p95 and tx/s against the US1 build.

**Acceptance Scenarios**:

1. **Given** a load run, **When** `pg_stat_statements` is inspected, **Then** `BEGIN` calls
   are ≈ the number of completions, not ≈ all requests (today: 155 694 for ~155 k requests).
2. **Given** a scan on a non-OPEN or missing transaction, **When** it is processed, **Then**
   the 404/409 error precedence is unchanged (the contract tests pass).

---

### User Story 3 — Cheaper HTTP handling behind the gateway (Priority: P3)

**Actors**: uvicorn in each service, nginx gateway

Services that only the gateway reaches should use the `httptools` parser instead of `h11`.
h11 accounts for ~26 % of a checkout worker's CPU.

**Why this priority**: +15 % tx/s under compose, mostly on scans, and low risk. nginx does not
forward the client's `Upgrade: h2c` header that forced `h11` (001 research R14); `d5_httptools`
served 177 k POSTs with 0 errors. It ranks below US1 and US2 because it doesn't help the
lock-bound `COMPLETE` path, and because it needs the gateway-mode test run to confirm.

**Independent Test**: The gateway-mode suite (`GATEWAY_URL=…`) passes. A load run shows
0 transport errors and scan p95 at or below the US2 build's.

**Acceptance Scenarios**:

1. **Given** the compose stack, **When** the full test suite runs in gateway mode, **Then**
   every test passes.
2. **Given** a 100-station load run, **When** it completes, **Then** there are zero transport
   errors or 5xx responses.

---

### User Story 4 — Correct the published performance explanation (Priority: P3)

**Actors**: README readers, graders

`load-client/reports/README.md` (Week 5) attributes ~0.2 ms to Docker port forwarding and
lists R8–R10 as the levers. The measured picture is different:
- port forwarding is ≈ 44 % of the gap at 100 stations
- the Unix socket (R8) is noise
- the hot-SKU lock is what amplifies every per-hop cost

**Acceptance Scenarios**:

1. **Given** the README, **When** it is read, **Then** its explanation of the Week 4 → Week 5
   gap matches `investigation.md` F1–F5 and links to it.

---

### Edge Cases

- **A basket has a single SKU.** The one-statement completion must still lock, check and
  decrement exactly that row.
- **A basket SKU has insufficient stock.** Nothing is modified. The response lists *every*
  short SKU (today's message format); the transaction stays OPEN and the locks are released.
- **Two completes of the same transaction race.** The transaction row lock (step 1) still
  serializes them. The second sees non-OPEN → `TRANSACTION_NOT_OPEN`.
- **A hot SKU sells out mid-run.** Fast 409s must not hold locks any longer than today.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The completion MUST take all stock locks in one deterministic global order
  (any total order; ascending SKU is kept). After the locks are granted, it MUST need at most
  one further client round trip before `COMMIT` completes.
- **FR-002**: The completion MUST preserve all-or-nothing semantics, the
  stock-never-negative guarantee, and alert insertion in the same transaction as the decrement.
- **FR-003**: The invariant `initial_stock − current_stock == SUM(qty of COMPLETED line items)`
  MUST hold on every load run, with zero deadlocks.
- **FR-004**: Scan and start MUST execute as a single round trip without changing their
  responses or error precedence.
- **FR-005**: Services reached only through the gateway MAY use `--http httptools`. Any port
  the load client can reach directly MUST keep `h11`.
- **FR-006**: The HTTP contract, the error catalog and the load client MUST NOT change.

### Key Entities

Unchanged from 001/005: `transaction`, `transaction_item`, `inventory_stock`,
`low_stock_alert`, `popular_window_snapshot`.

## Success Criteria *(mandatory)*

All measured with `harness/` on the same host and power state as the baseline.

- **SC-001**: Compose, 100 stations / 30 s / ample stock: tx/s ≥ 500 (baseline 409.0) and
  `COMPLETE` p95 ≤ 200 ms (baseline 432 ms).
- **SC-002**: Graded stress config: `COMPLETE` p95 ≤ 100 ms (baseline 249 ms) and items/s
  ≥ 5 800 (baseline 5 063).
- **SC-003**: Native, 100 stations: `COMPLETE` p95 ≤ 40 ms (baseline 124 ms).
- **SC-004**: `pg_stat_statements` `BEGIN` count ≤ 1.1 × completions.
- **SC-005**: Zero deadlocks, zero 5xx/transport errors, and invariant PASS on every run.

## Assumptions

- **Environment**: the measurement environment matches the investigation (M3 Pro,
  Docker Desktop VM 11 CPU / 13 GB, battery or AC used consistently, calibration run first).
- **Load profile**: the Zipf-skewed load profile is fixed by the unmodified client, so the
  hot-SKU contention is a property of the workload, not something to design away.
- **Out of scope**:
  - replacing SQLAlchemy with raw asyncpg on the hot path (Recommendation 4; larger change)
  - Docker Desktop port-forwarding overhead (a property of the macOS test setup)
