# Phase 0 Research: Self-Checkout Backend API

**Feature**: `001-checkout-backend` | **Date**: 2026-09-16

All Technical Context unknowns are resolved below. Every decision is grounded in the frozen
contract (`backend/spec.yaml`), the unmodified load client (`load-client/src/*.java`), or a
measurement from this host.

## Host baseline (the numbers the plan is calibrated against)

- Grading host: Apple Silicon, 11 cores, 18 GB RAM, macOS 15.6.
- Reference run of the Java mock, 10 stations / 60 s
  (`load-client/reports/report-20260916-185335.json`): 2 624 tx/s, 27 576 items/s,
  p50 0.259 ms, p95 0.412 ms, p99 0.744 ms, 0 % errors. That is in-process
  `ConcurrentHashMap` state with no persistence — the ceiling, not a target.
- Local tooling check: `psql`/`postgres` are **not installed**; Docker is installed but the
  daemon is **not running**. Provisioning PostgreSQL is a real setup task (see R3 and
  [quickstart.md](./quickstart.md)).
- Traffic shape: one transaction = 1 start + mean 10.5 scans + 1 complete ≈ 12.5 requests, so
  request rate ≈ 12.5 × tx/s.

---

## R1. Async stack: asyncpg + SQLAlchemy 2.0 async, not sync psycopg2

**Decision**: `async def` endpoints, SQLAlchemy 2.0 async engine over **asyncpg**. Add `asyncpg`
to dependencies; keep the pinned `psycopg2-binary` for the synchronous `scripts/seed.py` and
`scripts/verify_invariant.py`.

**Rationale**: Each request is one short DB round trip — almost pure I/O wait. Sync endpoints put
every request on Starlette's `run_in_threadpool` (40 threads by default), so 100+ concurrent
stations queue behind threads and pay GIL context-switch cost on top. asyncpg's binary protocol
and automatic prepared-statement caching are also measurably faster per query than psycopg2's
text protocol. Nothing in the spec requires the sync driver; the stack requirement is "FastAPI,
PostgreSQL, SQLAlchemy ORM", all of which hold.

**Alternatives considered**: (a) Sync `def` endpoints + psycopg2 + enlarged threadpool — simplest,
but thread-per-request is the wrong shape at 200 stations. (b) psycopg3 async — viable and would
replace both drivers, but SQLAlchemy 2.0.23 support is newer and asyncpg is the faster of the two
for this workload.

## R2. Process model: multiple stateless uvicorn workers

**Decision**: `uvicorn --workers N` (start at 4, tune 2–8) with uvloop + httptools, access log
disabled. **No mutable state in process memory** — every worker is interchangeable.

**Rationale**: CPython is single-core per process; one worker caps at roughly 1/11th of the host.
Keeping all state in PostgreSQL (including the analytics window, R6) is what makes multi-worker
safe — a per-process scan counter or window buffer would give each worker a different answer for
`/analytics/popular-items`. Worker count is bounded because the Java load client and PostgreSQL
share the same 11 cores during a run.

**Alternatives considered**: Single worker (simple, consistent, but leaves ~90 % of the host
idle); Gunicorn + uvicorn workers (equivalent here, extra dependency).

## R3. PostgreSQL deployment: native install, Unix socket, not Docker

**Decision**: Homebrew `postgresql@17` running natively, app connecting over the Unix domain
socket (`/tmp/.s.PGSQL.5432`). `docker-compose.yml` is provided as a fallback for a Linux host or
a machine without Homebrew.

**Rationale**: On macOS, Docker Desktop runs PostgreSQL inside a VM; both the network hop and the
filesystem go through that VM, adding roughly 0.1–0.5 ms per query — a large fraction of a sub-2 ms
budget. A native server on a Unix socket removes the TCP/loopback stack entirely (~50–100 µs
saved per query) and keeps WAL writes on the host APFS volume. Docker is also not currently
running on this host, so it is not the lower-friction option either.

**Alternatives considered**: Docker Compose as primary (portable, reproducible, but pays VM tax on
the one host the grades come from); Postgres.app (fine, equivalent to Homebrew, less scriptable).

**Server settings to apply** (justified in R11): `shared_buffers=4GB`, `work_mem=32MB`,
`max_connections=200`, `synchronous_commit=off`, `wal_compression=on`, `checkpoint_timeout=15min`,
`max_wal_size=8GB`.

## R4. Money as integer cents

**Decision**: Store `price_cents INTEGER`; keep `running_total_cents` / `total_amount_cents` as
`BIGINT`. Convert to a 2-decimal float only at the JSON boundary (`cents / 100`).

**Rationale**: A running total over 20 float additions drifts; the contract exposes
`runningTotal`, `unitPrice` and `totalAmount` as JSON doubles, and receipt totals must equal the
sum of their lines exactly. Integer arithmetic also lets the scan path do
`running_total_cents = running_total_cents + $n` inside SQL with no rounding question.
The mock's price formula `0.5 + (i % 47) * 0.35`, rounded to 2 dp, maps exactly onto cents.

**Alternatives considered**: `NUMERIC(10,2)` in PostgreSQL (exact, but slower arithmetic and an
extra Decimal→float conversion per response); Python `Decimal` end-to-end (same cost, no benefit
given the JSON contract is a double).

## R5. Identifiers: bigint identity PK, `tx-<id>` public string

**Decision**: `transaction.id BIGINT GENERATED ALWAYS AS IDENTITY` is the PK; the contract's
`transactionId` is the string `tx-<id>`. Inbound IDs are parsed back to the integer; anything that
does not match `tx-<digits>` returns `404 TRANSACTION_NOT_FOUND` without touching the database.

**Rationale**: The transaction row is inserted once and looked up ~12 times per transaction, all
on the hottest index in the system. Monotonic bigints append to the right edge of the B-tree
(no page splits, no random I/O) and compare in 8 bytes; random UUIDv4 keys scatter inserts across
the index and roughly double key size. The client treats `transactionId` as an opaque string, so
the format is free.

**Alternatives considered**: UUIDv4 (index fragmentation under a high insert rate); UUIDv7/ULID
(monotonic, but 16 bytes and needs a dependency or hand-rolled generator for no gain here).

## R6. Analytics: `transaction_item.id` as global scan sequence + snapshot table

**Decision**: The identity column on `transaction_item` **is** the global scan sequence — no
separate counter. A hopping window is recomputed out-of-band whenever a scan's sequence crosses a
multiple of `slideInterval` (500): the scan handler fires a background task, which takes
`pg_try_advisory_lock` (so exactly one worker recomputes), runs
`SELECT sku, count(*) FROM transaction_item WHERE id > windowEnd - 1000 AND id <= windowEnd GROUP BY sku ORDER BY count(*) DESC, sku LIMIT 200`,
and upserts the ranking plus `windowStart`/`windowEnd`/`computedAt` into a single
`popular_window_snapshot` row. `GET /analytics/popular-items` reads that one row and slices to
`limit`.

**Rationale**: Satisfies the contract's hopping-window semantics (recompute every N scans, report
window bounds) that the mock explicitly leaves as an exercise, while keeping the read path to one
indexed row fetch and the write path off the request's critical path. Because the state lives in
the database, every worker reports the same window — which a per-process deque could not do (R2).
The bounded 1 000-row range scan on the PK index costs well under a millisecond.

**Caveat, documented**: identity columns allocate per-session caches, so sequence values can be
consumed slightly out of order across workers and can leave small gaps after a rollback. The
window is therefore "the most recent ~1 000 scans", not an exact 1 000-element list. That is
within the contract's intent and matches the `windowStart`/`windowEnd` reporting model.

**Alternatives considered**: Recompute on every read (what the mock does — simple, but re-scans
1 000 rows per request and ignores `slideInterval`); a periodic timer task (decouples from scan
volume, so the window goes stale when traffic is slow and lags when it is fast); an in-process
ring buffer (fastest, but wrong under multiple workers and lost on restart).

## R7. Serialization: orjson + hand-built dicts on hot paths

**Decision**: Add `orjson` and use `ORJSONResponse` as the app's default response class. Hot
endpoints (`/transactions`, `/{id}/items`, `/{id}/complete`) return plain dicts with **no**
`response_model` validation; Pydantic models are used for request bodies and for documenting
responses via `responses=`. `GET /items` returns a pre-rendered `bytes` body built once at
startup.

**Rationale**: `response_model` re-validates and re-serializes every response through Pydantic,
which is a visible fraction of a 1 ms budget at ~12 requests per transaction. The response shapes
are fixed by a frozen contract and covered by contract tests, so runtime re-validation buys
nothing. The catalog body is ~120 KB of constant JSON fetched once per client run — rendering it
per request would be pure waste.

**Alternatives considered**: Keep `response_model` everywhere (self-documenting, measurably
slower); stdlib `json` (2–3× slower than orjson on these payloads).

## R8. Error contract: override FastAPI's defaults

**Decision**: Register handlers for `RequestValidationError` → `400 {"error":"INVALID_REQUEST",
"message":...}`, for `HTTPException` → `{"error":..., "message":...}`, and a catch-all 500
handler. Error codes: `INVALID_REQUEST` (400), `TRANSACTION_NOT_FOUND` / `SKU_NOT_FOUND` (404),
`TRANSACTION_NOT_OPEN` / `EMPTY_BASKET` / `INSUFFICIENT_STOCK` (409). Full table in
[contracts/error-catalog.md](./contracts/error-catalog.md).

**Rationale**: Out of the box FastAPI answers a malformed body with `422` and the shape
`{"detail": [...]}`; the contract requires `400` and `{error, message}` (`ApiError`). Left alone
this silently breaks FR-012. `POST /transactions` must also return **201**, not FastAPI's default
200 — the client asserts the exact status and raises on any mismatch.

**Alternatives considered**: None — the contract is frozen.

## R9. Scan path: one CTE, one round trip

**Decision**: Resolve SKU → name/price from the in-memory catalog cache (404 before any DB work),
then execute a single statement:

```sql
WITH tx AS (
  UPDATE transaction SET item_count = item_count + 1,
         running_total_cents = running_total_cents + :price_cents
   WHERE id = :tx_id AND status = 'OPEN'
  RETURNING id, item_count, running_total_cents
), ins AS (
  INSERT INTO transaction_item (transaction_id, sku, unit_price_cents)
  SELECT id, :sku, :price_cents FROM tx
)
SELECT item_count, running_total_cents FROM tx;
```

Zero rows back means the transaction is missing or not open; only then does a second cheap query
run to decide between `404` and `409`.

**Rationale**: `ScanResult` needs `itemCount` and `runningTotal` as of this scan. Denormalized
counters on the transaction row plus `RETURNING` give both without a `COUNT(*)`/`SUM()` over the
line items, and the CTE keeps insert + update in one network round trip and one implicit
transaction. The error path is the rare path, so paying an extra query there is free.

**Alternatives considered**: Insert then aggregate (two round trips, and `SUM()` grows with basket
size); compute totals in the app from session state (wrong under multiple workers).

## R10. Completion path: SKU-ordered locks + conditional batched decrement

**Decision**: One explicit DB transaction:

1. `SELECT status, station_id, started_at FROM transaction WHERE id = :id FOR UPDATE`
   → `404` if absent, `409 TRANSACTION_NOT_OPEN` if not `OPEN`. The row lock also serializes
   two concurrent completes of the same transaction, so no double decrement is possible.
2. `SELECT sku, count(*) AS qty FROM transaction_item WHERE transaction_id = :id GROUP BY sku ORDER BY sku`
   → `409 EMPTY_BASKET` if empty.
3. `SELECT sku FROM inventory_stock WHERE sku = ANY(:skus) ORDER BY sku FOR UPDATE` — acquire all
   stock locks in a single deterministic order.
4. `UPDATE inventory_stock s SET current_stock = s.current_stock - v.qty FROM (VALUES ...) AS v(sku, qty) WHERE s.sku = v.sku AND s.current_stock >= v.qty RETURNING s.sku, s.current_stock`.
   If the returned row count is less than the number of distinct SKUs → **rollback**, respond
   `409 INSUFFICIENT_STOCK`.
5. Insert `low_stock_alert` rows for any returned SKU that crossed the threshold on this
   transaction (`current_stock < threshold <= current_stock + qty`).
6. `UPDATE transaction SET status = 'COMPLETED', completed_at = now(), total_amount_cents = ...`.
7. Commit; build the receipt from step 2 plus the in-memory catalog.

**Rationale**: This is the correctness core the whole exercise is built around. Step 3's
`ORDER BY sku` gives every concurrent completion the same lock acquisition order, which makes
deadlocks structurally impossible; without it, a multi-row `UPDATE` locks rows in physical order
and two overlapping baskets can deadlock. Step 4's `WHERE current_stock >= v.qty` is what
guarantees stock never goes negative, and the all-or-nothing rollback is what FR-004 specifies.
Together they make `initial - final == completed line items` hold by construction.

**Alternatives considered**: (a) Optimistic `version` column with retry — under the Zipf skew the
rank-1 SKU is in ~74 % of baskets, so retry storms would dominate. (b) `SERIALIZABLE` isolation
with retry loops — same problem, plus serialization failures the client would see as errors.
(c) Per-unit decrements (one statement per scanned unit) — up to 20 round trips while holding
locks. (d) Clamping at zero like `MockServer.java` — produces a clean report by violating the
invariant that is actually graded.

## R11. Durability posture: `synchronous_commit=off`, everything else default

**Decision**: Run with `synchronous_commit=off`, `fsync=on`, regular (logged) tables.

**Rationale**: At ~500 tx/s each commit would otherwise wait on an fsync, making WAL flush the
dominant term in `COMPLETE_TRANSACTION` latency. With `synchronous_commit=off`, commits are
durable across a clean shutdown *and* a server-process crash; only an OS/hardware crash can lose
the last ~200 ms of commits. The spec's durability assumption ("survive server restarts") is met.

**Alternatives considered**: `UNLOGGED` tables (fastest writes, but they are truncated on crash
recovery — that breaks the persistence assumption outright); full `synchronous_commit=on`
(strictest, and the honest default for real payments, but it converts this benchmark into a
measurement of disk fsync latency rather than of the architecture).

## R12. Reproducibility harness: seed + verify scripts

**Decision**: `scripts/seed.py --reset` recreates the schema, loads 2 000 items using the mock's
exact SKU/name/price formulas, sets stock to `STOCK_PER_ITEM`, and clears transactions, alerts and
the window snapshot. `scripts/verify_invariant.py` asserts, per SKU,
`initial_stock - current_stock == SUM(qty of line items in COMPLETED transactions)` and
`current_stock >= 0`, printing a pass/fail summary plus the worst offenders.

**Rationale**: The README states the grading correctness check verbatim; making it a script means
every run's claim is evidence rather than assertion. Matching the mock's catalog formulas matters
beyond cosmetics: the load client derives its Zipf rank from *catalog response order*, so
`GET /items` must return SKUs in ascending order for popularity results to be comparable across
weeks.

**Alternatives considered**: Manual SQL checks (not repeatable); generating a random catalog
(breaks week-to-week comparability of the popular-items ranking).

---

## R13. Stock depletion under the load client's Zipf sampling

**Decision**: Treat the `409 INSUFFICIENT_STOCK` responses that appear late in a default run as
correct behaviour and expected evidence, not as a defect to engineer away. Keep `STOCK_PER_ITEM`
configurable so a companion high-stock run can also be submitted.

**Rationale**: `ItemSampler.java` weights item selection Zipf-like, so the rank-1 SKU takes
≈ 12.2 % of all scans (1/H₂₀₀₀). At the spec's default 10 000 units that SKU depletes after
≈ 82 000 scans ≈ 7 800 transactions — reached well inside a 60 s run at any workable throughput.
After that point roughly 74 % of baskets contain at least one unit of a depleted item, and FR-004
requires each of those completions to fail with `409 INSUFFICIENT_STOCK`. A nonzero
`COMPLETE_TRANSACTION` error count in the report is therefore the system working, and SC-007 is
written to allow exactly this: transport errors, timeouts, and server errors are disqualifying,
contract-defined 4xx from correct business rules are not.

**Alternatives considered**: Clamping stock at zero — what `MockServer.java` does with
`Math.max(0, current - 1)` — produces a cosmetically clean 0 % error rate but silently loses
units and breaks the graded invariant (INV-2 / SC-003). Rejected: correctness wins over a
prettier report. Raising the seeded stock so nothing depletes was rejected as the *primary* run
because it diverges from the spec's stated 10 000-unit scale; it is kept as a labelled companion
run (quickstart V7) so the V6 error rate is clearly attributable to correct behaviour.

---

## R14. HTTP parser: `--http h11`, not httptools

**Decision**: Run uvicorn with `--http h11`. This is a correctness requirement, not a
performance preference.

**Rationale**: `java.net.http.HttpClient.newBuilder()` defaults to `HTTP_2`, and the load client
(`ApiClient.java`) never overrides it. For an `http://` URL Java therefore attempts an h2c
upgrade, so **every** request carries `Connection: Upgrade, HTTP2-Settings` and `Upgrade: h2c`.
uvicorn's httptools parser classifies those as upgrade requests, logs
`WARNING: Unsupported upgrade request`, and never delivers the request body to the application.
`GET /items` is unaffected (no body to lose), but every `POST /transactions` arrives with an empty
body and is correctly rejected as `400 INVALID_REQUEST` — a 100 % error rate on
`START_TRANSACTION` and a report showing 0 transactions. The h11 parser handles the identical
request correctly and returns `201`.

Reproduced directly, independent of Java:

```bash
curl -X POST localhost:8080/transactions -H 'content-type: application/json' \
     -H 'Connection: Upgrade, HTTP2-Settings' -H 'Upgrade: h2c' \
     -H 'HTTP2-Settings: AAEAAEAAAAIAAAABAAMAAABkAAQBAAAAAAUAAEAA' \
     -d '{"stationId":"s"}'
# httptools -> 400 INVALID_REQUEST ("A JSON body is required.")
# h11       -> 201 Created
```

**Why this could not be fixed on the client side**: the load client is frozen — it is the fixed
instrument that makes week-to-week comparison meaningful, so the server adapts to it, never the
reverse (contracts/README.md).

**Cost**: h11 is a pure-Python parser and measurably slower than httptools. The measured V6 run
still lands inside the plan's target band on every percentile, so the trade is paid for. Later
architecture weeks that also front the contract with uvicorn must carry the same flag, or they
will hit the same 100 % `START_TRANSACTION` failure.

**Alternatives considered**: keeping httptools and stripping the upgrade headers before the
parser sees them (requires a socket-level shim below ASGI — fragile, and the body is already lost
by the time any middleware runs); serving HTTP/2 properly with Hypercorn (a different server for
one client quirk, and it changes the transport being measured relative to other weeks).

---

## Resolved unknowns summary

| Unknown from Technical Context | Resolution |
| --- | --- |
| Driver / concurrency model | R1 asyncpg + SQLAlchemy async; R2 multi-worker, stateless |
| Where PostgreSQL runs | R3 native Homebrew, Unix socket; Docker fallback |
| Money representation | R4 integer cents |
| ID scheme | R5 bigint identity, `tx-<id>` |
| Sliding-window mechanics | R6 identity column as scan sequence + snapshot row |
| Serialization cost | R7 orjson, no `response_model` on hot paths |
| Error-shape conformance | R8 explicit handlers, 400 not 422 |
| Hot-path SQL | R9 scan CTE; R10 completion lock order + conditional decrement |
| Durability vs latency | R11 `synchronous_commit=off` |
| Reproducible measurement | R12 seed + invariant scripts |
| Realistic performance target | Plan Technical Context → Performance Goals |
| Depletion-driven 409s | R13 (rank-1 SKU ≈ 12.2 % of scans) |
| Load client cannot POST a body | R14 `--http h11` (Java h2c upgrade vs httptools) |
