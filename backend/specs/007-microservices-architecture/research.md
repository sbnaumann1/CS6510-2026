# Research: Microservices Architecture

Each entry: **Decision**, **Rationale**, **Alternatives considered**. No `NEEDS CLARIFICATION`
items remain.

## R1. Coordination style for completion: orchestrated saga

**Decision**: Checkout orchestrates. Its `saga.py` runs: (1) local transaction: lock the
transaction, require `OPEN`, set `COMPLETING`, insert the `completion` row (state `RESERVING`);
(2) HTTP `reserve` to Inventory; (3) local transaction: `COMPLETED` + receipt fields, completion
state `COMPLETED`. Failures take the compensation paths in
[contracts/completion-saga.md](./contracts/completion-saga.md).

**Rationale**: The flow has one natural owner (the transaction), a fixed order and only two
participants. Orchestration keeps the state and the "what next" logic in one file, gives one
place to log (FR-020) and to test failures (FR-023). Inventory is the only step that can fail for
business reasons, so it runs first and Checkout's own commit last. That leaves a single
compensation (`release`).

**Alternatives**: *Choreography* (Checkout emits `CompletionRequested`, Inventory answers with
`StockReserved`/`StockRejected`, Checkout reacts). Needs a broker or hand-built callbacks, and
the shopper's synchronous `200`/`409` would have to wait on an event anyway, so it buys nothing
here. *Two-phase commit* across databases: forbidden by FR-012 and blocks on coordinator failure.

## R2. Inventory step: one idempotent local transaction; the hot lock is released before finalize

**Decision**: `POST /internal/reservations` runs, in Inventory's database, (a) read the
`stock_adjustment` row for the transaction id (replay returns the recorded result), then (b) the
spec 006 single-statement critical section, extended with an insert into `stock_adjustment` keyed
by transaction id (state `APPLIED`), then COMMIT. A unique-violation on a concurrent duplicate is
caught and treated as a replay. `DELETE /internal/reservations/{tx}` (release) flips `APPLIED` →
`RELEASED` and adds the units back in one statement. If no row exists it inserts a `RELEASED`
**tombstone**, so a `reserve` that arrives after a `release` (timeout race) is refused instead of
applied.

**Rationale**: Spec 006 showed throughput depends on how long the hot SKU's stock lock is held
(`SKU-000001` is in ~68 % of baskets). With the saga, that lock is held only inside Inventory
for one statement plus COMMIT, and is gone before Checkout's finalize runs. In 005 it was held
across Checkout's whole completion. The cost is one extra HTTP hop and one extra commit per
completion.

**Alternatives**: *Reserve/confirm (two-phase at the application level)*: holds stock in a
pending state until Checkout confirms. More states, an expiry job, and a second Inventory call
per completion, for no benefit when payment cannot fail. *Event-sourced stock*: heavier than the
problem.

## R3. Failure policy per saga step

**Decision**:

| Step outcome | Action |
| --- | --- |
| Inventory returns insufficient stock | Local tx: transaction → `OPEN`, completion row deleted. Return `409 INSUFFICIENT_STOCK` (nothing to undo) |
| Inventory call times out / connection error / 5xx | Retry the same `reserve` (idempotent) up to 2 more times within a 4 s budget. Still unknown → state `COMPENSATING`, send `release`, then transaction → `OPEN`, return `503` in the contract's error shape |
| `release` itself cannot be delivered | Leave `COMPENSATING`; recovery keeps retrying. The shopper already got `503` |
| Finalize local tx fails after a successful reserve | `COMPENSATING` → `release` → `OPEN` → `503` |
| Duplicate `complete` while `COMPLETING` | Joins the same saga: every step is idempotent, so both drivers converge on one outcome |
| `complete` on `COMPLETED` | `409` as in 005 (`TRANSACTION_NOT_OPEN`). A client that lost the original response can `GET /transactions/{id}` to see `COMPLETED` |

**Rationale**: Roll back when the outcome is unknown, roll forward only when stock is certainly
reserved. Rolling back is safe because `release` is idempotent and tombstoned, and the shopper
simply retries. "Retried completion returns the same receipt" (spec US3.5) is met while the
saga is in flight; after it finishes the contract's existing 409 applies (Spec Amendment 3 below).

**Alternatives**: Always roll forward, retrying `reserve` indefinitely, ties up shopper requests
and stock when Inventory is down.

## R4. Recovery loop

**Decision**: A background task in Checkout selects `completion` rows not updated for 10 s using
`FOR UPDATE SKIP LOCKED`, so several Checkout replicas can run it. `RESERVING` → treat as unknown →
`release`, transaction → `OPEN`. `RESERVED` → finalize (roll forward). `COMPENSATING` → resume
`release`. Runs every 2 s. Every action reuses the same idempotent calls as the live path.

**Rationale**: Satisfies FR-015 with no extra infrastructure and works the same after a crash or a
lost message.

**Alternatives**: Delayed message queue (needs a broker); relying on client retry (strands stock).

## R5. Analytics feed: in-memory pipeline that pulls scans from Checkout

**Decision**: Popular-items has always counted **scans**, including scans in transactions that
are never completed, and the existing contract tests assert that. So the feed is scan-driven, not
completion-driven. Analytics has **no database and no persistent store**. It is a derived read
model of Checkout's scan log, which is already durable in Checkout's `transaction_item` table.

- **Source.** Checkout exposes `GET /internal/scans?after=<scanId>&limit=<n>`, a primary-key range
  read over its own scan table returning `{scans: [{scanId, sku}], maxScanId}` in id order (only
  committed rows are visible). Checkout runs no relay, keeps no cursor and does not know Analytics
  exists.
- **Pipeline.** Analytics keeps 005's five-filter persistent pipeline (tail → window → aggregation →
  ranking → output). `TailIngest` keeps its contiguous-watermark and gap-grace logic, because
  concurrent checkouts still commit scan ids out of order. Only its source changes, from a SQL
  query to a call through `checkout_web.client` (a `ScanSource` seam keeps tests simple).
  `OutputFilter` publishes into an in-memory slot instead of upserting a snapshot row.
- **State.** The 1000-scan window, per-SKU counts and the latest top-200 ranking live in memory. A
  small rolling buffer of the last ≤ 1000 scans (`ScanWindow`) answers the "fewer scans than a
  window" read path that 005 answered with SQL (`windowStart` stays 0 until a full window exists).
- **Bootstrap and restart.** On start (and after any filter error) Analytics reads `maxScanId`, sets
  `b0 = maxScanId − maxScanId % 500`, and tails from `b0 − 1000`, exactly as 005's supervisor did
  from the database. Catch-up after downtime therefore costs about 1000 rows, not the number of
  scans missed. If `maxScanId` is ever lower than Analytics' cursor (Checkout's data was reset),
  it re-bootstraps.
- **No leader election.** 005's advisory lock existed so that replicas would not run duplicate
  pipelines writing one shared snapshot row. With in-memory state there is no shared write target:
  a second instance would compute the same window from the same source, which is redundant but
  never wrong. Deployment runs one instance (`WORKERS=1`, one container), so the supervisor loses its
  `_acquire` standby loop and dedicated connection.
- **Failure behaviour.** Checkout down: the tail retries with a 1 s backoff and Analytics keeps
  serving its last ranking from memory. Analytics restarted while Checkout is down: it cannot
  rebuild, so it serves the empty window until Checkout returns. This is accepted because the data
  is derived (spec edge cases). Analytics down: nothing else is affected, since nobody calls it
  during scans or completion.

**Rationale**: The only durable copy of a scan needs to exist once, in Checkout. A second copy in
an Analytics database held ~1.1 M rows after one stress run, was never pruned, and the pipeline only
ever needs the last 1000. Pulling flips the dependency to the natural direction (the read model
depends on the source), removes Checkout's relay, cursor table, advisory lock, Analytics client and
Analytics' ingest endpoint, and makes catch-up for free because the cursor lives with the reader.
The scan hot path is untouched (no extra write, no extra call). The cost is one indexed range query on
Checkout per poll (every 100 ms), the same query Analytics ran against the shared database in 005.

**Alternatives**: *Checkout pushes scans through a cursor relay into an Analytics database* (the
earlier plan; keeps a duplicate unbounded table, a relay, a cursor table and an ingest endpoint).
*Synchronous call per scan* (extra hop on the most frequent request, makes Analytics outages
break scans). *Analytics database with only the advisory lock removed* (keeps the duplicate
table). *Snapshot file for the last ranking* (would keep serving the old ranking through an
Analytics restart with Checkout down; deferred, add only if that edge case matters). *Broker*
(see R7).

## R6. Reference data: Catalog snapshot cache over the public API

**Decision**: Checkout, Inventory and Analytics each get catalog data from Catalog's public
`GET /items`, load it lazily on first need and keep it in memory. A miss on an unknown SKU
triggers at most one refresh per 5 s. Checkout stores the price on each scanned row. If Catalog is
down and the cache is empty, the dependent call returns `503`. If the cache is warm, scans keep
working.

**Rationale**: Scans are the most frequent request, so a per-scan Catalog call would add a hop
to every one of them. The catalog is fixed during a run (spec assumption). The cache is an
owned-elsewhere snapshot with a documented source (FR-009). A restarted service does not need
Catalog to be up until its first request.

**Alternatives**: Per-scan call (cost); copying catalog rows into each store at seed time
(duplicates ownership and drifts); startup dependency on Catalog (breaks independent restart).

## R7. Transport: synchronous HTTP/JSON, no broker

**Decision**: `httpx.AsyncClient` per target with connection keep-alive, connect timeout 0.5 s,
read timeout 2 s (reserve/release) and 5 s (catalog fetch). `X-Request-ID` is forwarded on every
call. Only idempotent calls are retried.

**Rationale**: One fewer container and failure mode, easy to fault-inject in tests via ASGI
transport, matches the existing nginx/uvicorn stack, and the request/response shape the
shopper needs for completion is synchronous anyway.

**Alternatives**: RabbitMQ/Kafka/NATS (operational weight and new failure modes for one async
feed that a pull handles); gRPC (new toolchain, no benefit at this scale).

## R8. Private store per service: two databases, one file, one derived in-memory state

**Decision**: Checkout and Inventory each get a `postgres:17` container with its own
role/password, on a network shared only with its owning service and that service's seed job.
Catalog has **no database**: its data is a fixed list written once by `seed-catalog` to
`catalog.csv` (header `sku,name,price_cents`) on a named volume. The volume is mounted read-write
into `seed-catalog` and **read-only** into `catalog`, and into nothing else. Catalog loads the file
into memory at startup and serves `GET /items` from it. Services also join a `mesh`
network (service ↔ service) and `edge` (gateway ↔ service). No service is attached to another
service's database network. Tuning: `synchronous_commit=off`, `wal_compression=on` kept;
`shared_buffers` sized per workload (checkout 512 MB, inventory 128 MB), `max_connections` per service pool.
Analytics has **no store** ([R5](#r5-analytics-feed-in-memory-pipeline-that-pulls-scans-from-checkout)).

**Rationale**: FR-007/FR-008 require "unreachable by network position, credentials or file access".
Separate instances mean a compromised or buggy service cannot read another's data, and a
database restart affects one service only.

Analytics needs no store because everything it holds can be recomputed from Checkout's scans.
A file is enough for Catalog because its data is read-only after seeding, has one reader and about 2000 rows. A
database there would add a container, a connection pool and a startup dependency for no benefit.

**Alternatives**: One server with separate databases (shared failure domain, spec Clarifications
reject it); separate schemas in one database (convention only); a Postgres instance for Catalog
(rejected above); SQLite file (adds a dependency to read a table that is loaded whole into memory).

## R9. Seeding and schema creation per service

**Decision**: Each service owns `seed.py` run by a one-shot compose job against its own database
(`seed-catalog`, `seed-checkout`, `seed-inventory`) using that service's role (Catalog's job has the volume instead). Analytics has no seed job.
Catalog writes `catalog.csv` (atomically: temp file, then rename); Inventory seeds stock for the same deterministic SKU list
(`SKU-%06d`, count from `CATALOG_SIZE`, quantity from `STOCK_PER_ITEM`, price/name formulas stay
in Catalog); Checkout creates an empty schema. Catalog's `--if-empty` means "file absent or empty". `--if-empty` semantics from 005 are
kept. No cross-database foreign keys exist.

**Rationale**: FR-010. The SKU naming formula is a shared *convention*, not shared data
or code.

**Alternatives**: One seed tool with four connections (violates FR-008 for the tool);
Inventory fetches the SKU list from Catalog at seed time (adds a startup dependency on Catalog).

## R10. Cross-store verification without cross-database access

**Decision**: `scripts/verify_invariant.py` calls read-only internal reports:
Checkout `GET /internal/reports/sold-by-sku`, Inventory `GET /internal/reports/stock`,
Analytics `GET /internal/reports/cursor`, plus Checkout `GET /internal/reports/scan-count`.
It checks per SKU `initial − current == sold`, no negative stock, no transaction left
`COMPLETING`, no non-terminal `completion` rows, and (after waiting for Analytics to catch up)
Analytics' `lastScanId` equals Checkout's `maxScanId`.

**Rationale**: Keeps FR-008 literal: the checker is not a back door into any database. Also tests
that each service can describe its own data.

**Alternatives**: A tool holding credentials for both databases (convenient, but breaks the
boundary it is checking).

## R11. Gateway: stays nginx, adds request id and hides internal routes

**Decision**: Same path routing. Add `proxy_set_header X-Request-ID $request_id;`, and
`location /internal/ { return 404 <contract-shaped json>; }` (the default route would otherwise
forward `/internal/...` to Checkout). The in-process test twin in `tests/_dispatch.py` gets the
same rule.

**Rationale**: FR-003 and FR-020. A correlation id appears on every request from the first hop
without application code.

**Alternatives**: A Python gateway with aggregation logic (no need, and slower).

## R12. Observability

**Decision**: Every service logs one JSON line per saga step and per outbound call:
`request_id`, `service`, `step`, `tx`, `outcome`, `elapsed_ms`. `request_id` comes from
`X-Request-ID` (generated if absent, e.g. in direct-port tests). Background work (recovery,
Analytics' tail) logs its own generated id plus the transaction id.

**Rationale**: FR-020, SC-008. Failures are findable by grep across container logs.

**Alternatives**: Distributed tracing stack (Jaeger/OTel collector): more containers than the
problem needs.

## R13. Test strategy

**Decision**: (a) Existing contract/integration tests keep their assertions. Fixtures change:
two test databases (Checkout, Inventory), a temporary catalog file (`CATALOG_FILE`), each database service's engine bound to its own URL, and a mesh transport that
routes inter-service calls to in-process apps. Tests that inserted directly into the old shared
tables (for example `test_popular_items`'s direct scan inserts) now insert into Checkout's
scan table through the Checkout session fixture, and Analytics tests poll once through a
`poll_analytics` hook instead of waiting for the 100 ms tail, which is a fixture change, not an
assertion change. (b) New `tests/failure/`: the mesh transport can drop, delay or fail the Nth call
to a target, covering each saga step and each recovery path. (c) Compose drills in the quickstart
stop and restart real containers. (d) Load runs through the real gateway.

**Rationale**: FR-023 needs deterministic fault injection, which an in-process transport gives
without flaky timing; the compose drills prove the real network behavior.

**Alternatives**: Toxiproxy for everything (slower, flakier in CI); mock the inventory client
(would not exercise real serialization or idempotency).

## R14. Performance expectations and mitigation

**Decision**: Expect completion latency to rise by one HTTP hop and one extra local commit
(three Checkout round trips in two transactions, plus Inventory's own). The step-1 local
transaction is a single statement where possible, the Inventory call reuses a keep-alive
connection, and scans are unchanged (cache hit, one CTE). Measure against 005's 191 tx/s stress
baseline (SC-009: ≥ 70 %). Worker and pool sizes are tuned through compose environment variables
within each database's connection limit.

**Rationale**: The 006 investigation showed completions are lock-bound, not CPU-bound, and the
saga shortens the lock window, so a net regression is not expected.

**Alternatives**: Merge Checkout and Inventory to remove the hop (rejected by the spec; revisit
only if the 70 % target is missed and the cause is the hop).

## Spec Amendments Made During Planning

Four findings from reading the 005 code and tests contradicted wording in the spec. All are now
corrected in `spec.md`:

1. **Analytics is fed scans, not completed sales** (R5). The existing contract tests scan without
   completing and expect popular-items to count those scans.
2. **Catalog data is a cached snapshot, not a per-scan call** (R6), so scans survive a Catalog
   outage once the cache is warm.
3. **A retried `complete` on an already-completed transaction keeps the contract's `409`**
   (R3), rather than returning the same receipt. Retries *during* the saga converge on one
   outcome, and stock is reduced once either way.
4. **Analytics needs no database** (R5). Its state is derived from Checkout's scans, so it
   keeps a rolling window in memory and pulls scans from Checkout. This removes the Analytics
   database, the scan relay and cursor table, the ingest endpoint and the advisory lock, and
   introduces the accepted edge case of an Analytics restart while Checkout is down.
