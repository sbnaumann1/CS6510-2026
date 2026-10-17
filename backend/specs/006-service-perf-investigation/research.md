# Research: Hot-Path Latency Reduction (spec 006)

> **Evidence branch.** The load/profiling harness (`harness/`) and the raw run results
> (`results/`) mentioned in this document are not in this branch's tree. They are kept on
> branch `worktree-perf-hot-path-latency` (commit `6987174`), under
> `backend/specs/006-service-perf-investigation/`. To use them, run
> `git restore --source=worktree-perf-hot-path-latency -- backend/specs/006-service-perf-investigation/harness backend/specs/006-service-perf-investigation/results`
> and add `.perf-runs/` (the harness's output directory) to `backend/.gitignore` and
> `backend/.dockerignore`.

The measurements behind each decision are in [investigation.md](./investigation.md) (F1–F5).
This file records the design decisions made from them. Every unknown in the plan's Technical
Context is resolved here; none remain open.

## R1 — Completion critical section: one statement after the transaction row lock

**Decision**: Replace steps 3–6 of `complete_transaction` (`lock_stock`, `decrement_stock`,
`emit_crossings`/`insert_alerts`, `complete_transaction`) with one repository call,
`repos.checkout.complete_basket`. It executes a single data-modifying CTE:

| Part | What it does |
|---|---|
| `v` | the basket, as `unnest(CAST(:skus AS text[]), CAST(:qtys AS int[]))` |
| `locked` | `AS MATERIALIZED`: `SELECT sku, current_stock … WHERE sku = ANY(:skus) ORDER BY sku FOR UPDATE` |
| `ok` | true iff every basket SKU was locked and has `current_stock >= qty` |
| `dec` | the decrement, guarded by `(SELECT ok FROM ok)` |
| `alerts` | inserts a row for each `dec` row that crossed the threshold: `current_stock < :thr <= current_stock + qty` |
| `fin` | sets the transaction to `COMPLETED`, guarded by `ok` |

The final `SELECT` returns `ok`, `completed_at`, and `short`: the sorted SKUs that are
missing or short.

Steps 1–2 (lock the transaction row, read the basket) stay as they are. When `ok` is false,
nothing was modified. The service raises `INSUFFICIENT_STOCK` listing `short`, and
`session.begin()` rolls back, releasing the locks.

**Rationale**

- **Hold time is the bottleneck.** Throughput is capped by how long the hot-SKU lock is
  held, and that hold time is client round trips (investigation F1). This leaves exactly
  one round trip, `COMMIT`, after the locks are granted.
  - Measured with the prototype, natively: +13 % tx/s, `COMPLETE` p95 124 → 30 ms.
  - Under compose: +31 %, p95 432 → 172 ms.
- **The locks are taken before any write** (`EXPLAIN`, generic plan, 2 000-SKU catalog).
  The `dec` and `fin` updates sit under a **One-Time Filter on the `ok` InitPlan**, and `ok`
  scans `locked` (`LockRows` ← `Sort by sku` ← `Bitmap Index Scan inventory_stock_pkey`).
  So every stock lock is acquired in SKU order before any row is modified: the same
  deterministic order as today, and deadlock-free (FR-001).
- **Stock checks see current values under READ COMMITTED.** `FOR UPDATE` returns the latest
  committed row version after waiting (EvalPlanQual). `dec`'s `UPDATE` re-checks the
  locked row's latest version the same way. So the `>= qty` check and the decrement both
  see the current stock, and the stock-never-negative guarantee holds (FR-002).
- **All side effects are visible to the statement's own `SELECT`.** Data-modifying CTEs run
  to completion whether or not they are referenced, so `alerts` is always executed.
- **Behaviour matches today's.** The alert crossing rule is today's `emit_crossings`
  condition (once per descent). `short` is the same set as today's
  `set(skus) − decremented`, sorted, so the `INSUFFICIENT_STOCK` message is identical.
- **One statement text for every basket size.** `unnest` of two arrays means asyncpg
  prepares one statement per connection. Today `decrement_stock` interpolates a `VALUES`
  list per basket size, giving up to 20 variants per connection, and the prototype did the
  same. Removing that is the likely reason the prototype showed no uncontended saving
  (F4); this version should do slightly better.

**Alternatives considered**

- **Keep the statements separate, pipelined.** asyncpg has no pipelining for independent
  statements in one transaction; rejected.
- **A PL/pgSQL function.** It also gives one round trip, but adds a migration and moves
  business logic into the schema, against 005's "schema unchanged" stance. The CTE is
  plain SQL in the repo module, where the boundary check already looks; rejected.
- **Descending lock order.** Measured: no gain (F1). Waiters move from `Lock:tuple` to
  `Lock:transactionid`; rejected. Ascending order stays.
- **Merging steps 1–2 into the statement.** It would save one round trip, but *before* the
  stock locks, so it doesn't shorten the hold. It would also blur the
  `TRANSACTION_NOT_FOUND` → `TRANSACTION_NOT_OPEN` → `EMPTY_BASKET` precedence; deferred.

## R2 — Single-statement requests run in autocommit

**Decision**

- `checkout_data` gains an autocommit engine view and its own session factory, and a
  FastAPI dependency to go with it:
  - `autocommit_engine = engine.execution_options(isolation_level="AUTOCOMMIT")`
  - `AutocommitSessionLocal`
  - `get_autocommit_session()`
- The engine view shares the same pool. Only the Checkout `start` and `scan` routes use it.
- `complete`, `get`, the sweeper and all other services keep `get_session` (transactional).

**Rationale**

- **Round trips.** Scan and start each execute one atomic statement. Wrapping it in
  `BEGIN … COMMIT` costs two extra round trips: 155 694 `BEGIN`s for ~155 k requests (F4).
  - Natively that is 0.056 ms of a 0.73 ms scan.
  - Under compose, per-round-trip cost is higher and feeds the worker queueing in F3.
- **Probe results** (`DB_POOL_SIZE=1`, so the same physical connection is reused):
  1. A row inserted through an autocommit session is visible to another connection before
     `commit()` is called, and the driver reports no open transaction.
  2. A default session's insert is invisible until commit, and gone after rollback.
  3. Default sessions on the same pooled connection stay transactional afterwards:
     SQLAlchemy resets the isolation level when the connection is returned to the pool.
- **Error paths stay correct.** Scan's not-open/missing path (`rollback()` then `get_status`)
  still works: `rollback()` is a no-op in autocommit, and the failed CTE modified nothing,
  because the `UPDATE`'s `WHERE status = 'OPEN'` matched no row.
- **Conventions stay intact.** The repository rule ("repos never begin/commit/rollback; the
  caller owns the boundary") is unchanged, and services keep calling `session.commit()`,
  which is a no-op in autocommit. The choice of boundary is made where it belongs: in the
  route's dependency.

**Alternatives considered**

- **Raw asyncpg connections for scan and start.** That would also skip SQLAlchemy's
  ~0.14 ms, but it bypasses the shared session API and the repository signatures. Out of
  scope (spec Assumptions; investigation Recommendation 4).
- **A per-request `execution_options` switch on the session.** Changing isolation on an
  already-checked-out connection works, but scatters configuration across call sites.
  A dedicated factory is clearer and easier to test.

## R3 — `httptools` behind the gateway, with the gateway explicitly clearing `Upgrade`

**Decision**

- Each service image runs uvicorn with `--http ${UVICORN_HTTP}`.
  `ENV UVICORN_HTTP=httptools` in all four service Dockerfiles; `h11` stays one
  environment variable away.
- `gateway/nginx.conf` adds `proxy_set_header Upgrade "";` and
  `proxy_set_header HTTP2-Settings "";` next to the existing `Connection ""`.

**Rationale**

- **Gain.** h11 is ~26 % of a checkout worker's CPU (F3). `httptools` measured +15 % tx/s
  under compose, with 0 errors across 177 k POSTs (`d5_httptools`).
- **Why the 001 R14 hazard is gone.** The hazard was the Java client's `Upgrade: h2c`
  making httptools drop request bodies. Today nginx doesn't forward `Upgrade` because it is
  a hop-by-hop header. The explicit `proxy_set_header` turns that default into a stated
  guarantee in the config, which `test_gateway_routes.py` can assert.
- **Direct ports are safe.** Ports 8081–8084 are reached only by `scripts/check_independence.sh`
  (curl, which never sends `h2c` for `http://` URLs) and by humans debugging. The contract's
  client uses one base URL, the gateway (FR-008 of 005).
- **This revisits 005 R10.** R10 kept h11 for "any client" on the direct ports. That
  generality has a measured cost and no consumer.

**Alternatives considered**

- **httptools for Checkout only.** It's the only service on the hot path, so the gain
  elsewhere is ~0. But it would split the images' runtime configuration for no benefit;
  uniform is simpler.
- **Keep h11.** It leaves 15 % on the table; rejected.

## R4 — Lock order stays ascending by SKU

**Decision**: No change.

**Rationale**: Descending order was measured at −1 % (compose) / +3 % (native), with mixed
tail effects (F1). Any total order is deadlock-free. Once R1 collapses the critical
section, the order matters even less.

## R5 — Shared-library versioning

**Decision**: Bump `checkout-data` from 0.5.0 to **0.6.0**: it gains
`complete_basket`, `autocommit_engine`, `AutocommitSessionLocal` and
`get_autocommit_session`, and loses `lock_stock`, `decrement_stock`, `insert_alerts` and
`complete_transaction` from `repos.checkout`. Then re-run `uv lock`.

`checkout-web` is unchanged. The per-image `requirements.lock` files export third-party
packages only (`--no-emit-workspace`), so they stay the same and
`test_service_locks.py` keeps passing.

**Rationale**: 005 treats `checkout-data` as the versioned coupling point. A changed
repository surface is a minor version.

## R6 — Measurement protocol for the success criteria

**Decision**

- Validate SC-001–SC-004 with the investigation harness (`harness/run_exp.sh`,
  `native_exp.sh`) in the same session as a calibration baseline:
  1. Rebuild the images.
  2. Run `base100` on `main`, then on this branch, back to back, on the same power state.
  3. Then run the graded stress config once each.
- Before that, parametrize the harness paths: `J`, `BACK`, `LC` and the `*.yaml` mount
  paths become environment variables with repo-relative defaults, so anyone can re-run it.
- The single-statement prototype overlay (`proto.yaml`) becomes unnecessary once R1 ships,
  but is kept as investigation evidence.

**Rationale**: The investigation's noise floor is ~5 %, and absolute numbers depend on host
power state (investigation, Method). Only a same-session A/B is meaningful.

## R7 — README correction (US4)

**Decision**

- Rewrite the "Throughput is 69 %/71 % of Week 4" paragraph in
  `load-client/reports/README.md` (Week 5) to match investigation F1–F5:
  - the 005 code matches the monolith natively
  - port forwarding is ≈ 44 % of the gap at 100 stations
  - VM overhead is ≈ 52 %
  - the Unix socket (R8 of 005) is noise
  - the hot-SKU lock amplifies every per-round-trip cost
- Link the investigation from it.
- Add a short spec-006 note to `backend/README.md` describing the completion statement and
  the autocommit routes.

**Rationale**: The current text names R8–R10 as "the levers that would close the gap". Two
of the three were measured as no-ops or minor.

## R8 — Out of scope (recorded so tasks don't drift)

- **Replacing SQLAlchemy with raw asyncpg on hot paths** (investigation Recommendation 4).
- **Docker Desktop port forwarding**: a property of the macOS test setup.
- **Changing the graded runs to an in-network client**: FR-008 of 005 forbids changing the
  client invocation.
