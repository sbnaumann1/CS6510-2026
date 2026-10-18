# Codewalk Outline — Self-Checkout Backend

Outline for a **15–20 minute** code walkthrough followed by Q&A. It follows the course
guide: (1) architecture and design, (2) deep-dive walkthrough, (3) reflections.
Slides marked **[OPTIONAL]** are the first to cut. Core path is about **17 minutes**.

| Part | # | Slide | Min | Status |
| --- | --- | --- | --- | --- |
| **1. Architecture & design** (4 min) | 1 | The problem and the contract | 0.5 | core |
| | 2 | Architecture and data flow | 2 | core |
| | 3 | Key design decisions and trade-offs | 1.5 | core |
| **2. Deep dive** (10 min) | 4 | Entry point and core flow: start and scan | 1.5 | core |
| | 5 | Completion: concurrency-safe stock decrement | 2 | core |
| | 6 | **Popular items: windowing logic** | 2.5 | core |
| | 7 | **Popular items: persistence and recovery** | 2 | core |
| | 8 | Design patterns in practice | 1 | core |
| | 9 | Error handling and edge cases | 1 | core |
| **3. Reflections** (3 min) | 10 | Challenges and how we solved them | 1.5 | core |
| | 11 | Results | 0.5 | core |
| | 12 | Future improvements | 1 | core |
| Backup | 13 | Boundary enforcement and testing | 1 | optional |
| | 14 | Gateway details | 1 | optional |
| | 15 | Demo commands, Q&A, file map | — | backup |

**Presenting checklist (from the guide):**
- Zoom the IDE font way up; close the project tree, terminal and toolbars.
- Open each diagram *before* switching to code. Prepare tabs in the order of the slides.
- Tell one story: *"Station 7 scans three items, pays, and meanwhile the popular-items ranking updates."*
- Do not read code line by line. Show the one block that carries the idea.

---

# Part 1 — Architecture & Design Overview (4 min)

## Slide 1 — The problem and the contract (0.5 min)

- One frozen API contract (`spec.yaml`, OpenAPI 3.0.3). Only the server architecture changes week to week, and the same Java load client runs unmodified every time.
- Domain: a station starts a transaction, scans items one at a time, then completes to pay.
- Stock is decremented **at completion**, never at scan.
- Graded correctness property: stock never goes negative, and `initial_stock − current_stock` equals the sum of completed decrements, even under many concurrent stations.
- Week 5 goal: split the monolith into independently deployable services by business function.

**Show:** the domain-model section of `spec.yaml` (lines ~10–45).

## Slide 2 — Architecture and data flow (2 min)

**Open the diagram first:** the *Infrastructure* mermaid diagram in `backend/README.md`.

- Four domain services, one PostgreSQL 17 database, one nginx gateway on `:8080`.

| Service | Business function | Endpoints |
| --- | --- | --- |
| Catalog | what can be sold | `GET /items` |
| Checkout | basket from start to payment | `POST /transactions`, `/transactions/{id}/items`, `/transactions/{id}/complete`, `GET /transactions/{id}` |
| Inventory | stock that needs attention | `GET /inventory/low-stock` |
| Analytics | buying behaviour | `GET /analytics/popular-items` |

- **Data flow, in the story's order:**
  1. The client calls the gateway, and nginx routes by path.
  2. Checkout writes `transaction`, `transaction_item`, `inventory_stock` and `low_stock_alert`.
  3. Inventory reads stock and alerts.
  4. Analytics **tails** `transaction_item` and writes one snapshot row.
- **Services never call each other.** There are no arrows between them. They meet only at tables.
- Layering inside each service: `api.py` (HTTP) → `service.py` (business rules, transaction boundary) → `repos/<service>.py` (one SQL statement per function).

## Slide 3 — Key design decisions and trade-offs (1.5 min)

| Decision | Why | Trade-off |
| --- | --- | --- |
| Four services by business function | Independent deploy, restart and failure: when Analytics is down, only its route returns a JSON `503` | More moving parts than a monolith |
| One shared database | Strongly consistent stock; completion is one ACID transaction | Schema coupling between services |
| **Declared table ownership** (`ownership.TABLES`) | Makes the coupling visible and checkable: one writer per table, explicit readers | A schema change means bumping `checkout-data` and redeploying that table's readers |
| Services communicate through data, not calls | No service-to-service failure modes; Analytics catches up after downtime | Polling latency (`popular_poll_ms`) instead of push |
| All SQL in one package, one repo module per service | Boundary rules are checkable by script | Shared library is distributed into every image |

**Show:** `packages/checkout_data/src/checkout_data/ownership.py:28-36` (the `TABLES` registry). Mention that `scripts/check_boundaries.py` fails the test suite on cross-service imports, network clients, SQL outside `checkout_data`, and ownership violations.

---

# Part 2 — Deep-Dive Code Walkthrough (10 min)

## Slide 4 — Entry point and core flow: start and scan (1.5 min)

**Story:** *Station 7 starts a transaction and scans a SKU.* Trace one request from the gateway to the database.

1. **Gateway:** `gateway/nginx.conf`, `location /transactions` → `proxy_pass http://checkout`.
2. **Entry point:** `services/checkout/src/checkout_service/main.py` builds the app with `create_service_app` (`packages/checkout_web/.../app.py`), which registers routers, error handlers and `/healthz`.
3. **Handler:** `services/checkout/src/checkout_service/api.py` (scan route) parses the body by hand, calls `svc.parse_tx_id` so a malformed id is a 404 with no DB work, and delegates.
4. **Service:** `service.py:60-97` (`scan_item`) resolves the SKU from the **in-memory catalog cache** (so `SKU_NOT_FOUND` precedes `TRANSACTION_NOT_OPEN`, and pricing needs no query) and then calls the repo.
5. **Repository:** `repos/checkout.py:27-45` (`_SCAN`). One CTE bumps the counters, inserts the line and `RETURNING`s both, so a scan is **one round trip**.
6. **Why it is fast:** the route uses an autocommit session (`get_autocommit_session`), so there is no `BEGIN`/`COMMIT`. `WHERE status = 'OPEN'` means a closed transaction matches no row and there is nothing to undo.

**Hand-off to the next slide:** `transaction_item.id` is the global scan sequence, and it is the cursor Analytics tails (slide 6).

## Slide 5 — Completion: concurrency-safe stock decrement (2 min)

**The core logic that protects the graded invariant.**

`complete_transaction` (`service.py:109-175`), inside one DB transaction:
1. `FOR UPDATE` on the transaction row, which serializes double-completes.
2. Read the basket as `(sku, qty)` in SKU order.
3. `complete_basket`: **one SQL statement** that locks, checks, decrements, alerts and finalizes.
4. If `ok` is false, nothing was modified and the API returns `409 INSUFFICIENT_STOCK` with the short SKUs.

Why the statement is correct (`repos/checkout.py:57-110`, `_COMPLETE_BASKET`):
- **No deadlock:** `locked` takes every stock row `FOR UPDATE` in **SKU order**.
- **No partial update:** `locked` is `MATERIALIZED` and read by `ok`. `dec` and `fin` run only when `ok`, so all locks are granted before anything changes.
- **Never oversell:** under READ COMMITTED the lock and the `UPDATE` see the same latest stock. `CHECK (current_stock >= 0)` is a backstop.
- **Alert once per descent:** `current_stock < :thr AND :thr <= current_stock + qty` fires only on the crossing, in the same transaction.

**Show:** the CTE chain top to bottom: `locked` → `ok` → `dec` → `alerts` → `fin`. If short on time, show only `locked` and `ok`.

## Slide 6 — Popular items: windowing logic (2.5 min)

**The specific case the guide asks for.** *How does the popular-items windowing logic work?*

**The requirement:** the ranking covers the most recent **1 000 scans** (`POPULAR_WINDOW_SIZE`), recomputed every **500 scans** (`POPULAR_SLIDE_INTERVAL`). This is a hopping window counted in scans, not transactions or time.

**Key idea:** the window is defined by **scan id**, not by clock time. `transaction_item.id` is a global, increasing sequence, so window `k` is exactly `(boundary − 1000, boundary]` where `boundary` is a multiple of 500.

**Show the pipeline diagram first, then the code:**

```text
TailIngest ─q1─▶ WindowFilter ─q2─▶ AggregationFilter ─q3─▶ RankingFilter ─slot─▶ OutputFilter
 (DB tail)       (slide by id)      (running counts)        (top-200)              (latest-wins, commit)
```

| Stage | What it does | Code |
| --- | --- | --- |
| `TailIngest` | Polls `transaction_item` for ids above its watermark. Emits `ScanData` in id order, and a `Watermark(b)` for each slide boundary it has fully passed | `pipeline.py:94` |
| `WindowFilter` | Holds a `deque` of scans. On a watermark it emits a `Slide(window_start, window_end, added, evicted)` | `pipeline.py:188` |
| `AggregationFilter` | One running `Counter`. Adds the SKUs that entered and subtracts the ones that left, so a slide costs O(slide), not O(window) | `pipeline.py:217` |
| `RankingFilter` | Sorts by `(-count, sku)`, which is the deterministic tie-break, and keeps the top 200 | `pipeline.py:236` |
| `OutputFilter` | Writes the ranked window (next slide) | `pipeline.py:291` |

**The subtle part, and the best thing to explain: out-of-order commits.**
- Sequence ids are handed out at insert time, but rows become visible at *commit* time. Row 1001 can be visible before row 1000.
- `TailIngest` therefore advances only over a **contiguous** prefix (`last_id`), parking early arrivals in `pending` (`_drain`, `pipeline.py:141`).
- A watermark for boundary 1000 is emitted only once every id ≤ 1000 is accounted for, so a window is never computed with a scan missing.
- An id that never commits (a rolled-back transaction) would stall the pipeline, so after a grace period (`popular_gap_grace_ms`, 1 s) the hole is declared a permanent gap, logged (`gap skipped id=…`) and skipped (`_skip_expired_hole`, `pipeline.py:150`).

**Show:** `TailIngest._drain` and `_skip_expired_hole`, then the `WindowFilter.run` body (a dozen lines).

## Slide 7 — Popular items: persistence and recovery (2 min)

**How the result is persisted, read, and recovered.**

**Persistence: one snapshot row.**
- `popular_window_snapshot` is a **single-row table** (`CHECK (id = 1)`). Columns: `window_size`, `slide_interval`, `window_start`, `window_end`, `computed_at`, and `ranking` as **JSONB** (top 200, rank is index + 1). See `models.py:147-160`.
- `OutputFilter` writes it with `INSERT … ON CONFLICT (id) DO UPDATE` (`repos/analytics.py:34`, `upsert_snapshot` at line 86) and commits.
- **Latest wins:** `LatestSlot` (`pipeline.py:263`) is a one-item mailbox. A newer window replaces an unconsumed older one, so a burst of slides costs one write, not a backlog. `TERMINATE` is kept separately so shutdown never drops the final window.
- **Never go backwards:** `OutputFilter` skips any window whose `window_end ≤ last_written_end`.

**Read path:** `popular_items.read` (`popular_items.py:60`) is a primary-key fetch of the snapshot. Names and ranks are attached at read time from the catalog cache, so the stored blob stays small. Every worker and replica serves the same ranking.

**Edge case: fewer scans than one slide.** There is no snapshot yet, so `read` computes the window on demand over whatever exists (`popular_items.py:71`). This is why `windowStart` can be 0.

**Recovery: the supervisor** (`supervisor.py:45`).
- Holds a **PostgreSQL advisory lock**, so exactly one pipeline runs across replicas. Others stand by and retry.
- On start (`_run_pipeline`, `supervisor.py:96`) it reads `max_scan_seq`, aligns down to a slide boundary, and starts the window one window-size earlier. A restarted Analytics **rebuilds the window from the database** and catches up on scans made while it was down. Nothing is lost, because the scan record is the source of truth and the snapshot is a derived cache.
- If a filter raises, the supervisor logs, backs off and rebuilds the whole pipeline.

**Show:** the `_UPSERT` SQL, `LatestSlot`, and `supervisor._run_pipeline`.

## Slide 8 — Design patterns in practice (1 min)

| Pattern | Where | What it buys |
| --- | --- | --- |
| **Pipes and Filters** | `pipeline.py`: five filters joined by `asyncio.Queue`s | Each stage has one job and its own state; bounded queues give backpressure |
| **Poison pill (sentinel)** | `TERMINATE` flows downstream at shutdown only | Clean drain without cancelling mid-window |
| **Mailbox / latest-wins** | `LatestSlot` | A slow writer never backs up the pipeline |
| **Repository** | `repos/<service>.py`: one statement per function, none commit | SQL lives in one place; the service owns the transaction boundary |
| **Layered / service layer** | `api.py` → `service.py` → `repos/` | HTTP, business rules and SQL are separately testable |
| **Dependency injection** | FastAPI `Depends(get_session)`; `TailIngest` takes `session_factory`, `repo`, `clock` and `sleep` as constructor arguments | Pipeline tests run with a fake clock, with no real waiting |
| **Leader election** | `PipelineSupervisor` plus a Postgres advisory lock | One active pipeline without extra infrastructure |
| **Gateway / facade** | nginx | One base URL over four services |
| **Centralized error mapping** | `ApiError` plus handlers in `checkout_web/errors.py` | Every failure leaves in the contract's shape |

**Show:** pick the two strongest for time: Pipes and Filters (`pipeline.py` docstring and `_run_pipeline`'s queue wiring) and injection in `TailIngest.__init__`.

## Slide 9 — Error handling and edge cases (1 min)

**Contract errors** (`packages/checkout_web/src/checkout_web/errors.py`):
- Anything raises `ApiError(code, message)`. One handler renders `{"error", "message"}` with the right status.
- FastAPI's default 422 is remapped to the contract's `400 INVALID_REQUEST`.
- Unhandled exceptions: the traceback goes to the log, **never the response body**, and the client gets `500 INTERNAL_ERROR`.
- Precedence is deliberate: `SKU_NOT_FOUND` before `TRANSACTION_NOT_OPEN`; a malformed id is a 404 before any query.

**Concurrency and consistency:**

| Edge case | Handling |
| --- | --- |
| Two completes of the same transaction | `FOR UPDATE` on the transaction row serializes them; the second sees `COMPLETED` → `409` |
| Many baskets contending for the same SKUs | Locks in SKU order, so no deadlock |
| Insufficient stock | One statement checks every line; nothing is modified; `409` lists the short SKUs |
| Scans committing out of order | Contiguous watermark in `TailIngest` |
| A scan id that never commits | Gap skipped after the grace period |
| Analytics process restarts | Window rebuilt from the database |
| Two Analytics replicas | Advisory lock; one leader |
| A service is down | Gateway returns a contract-shaped `503` for that service's routes only |
| Abandoned transaction | `sweeper.py` cancels old `OPEN` transactions; no stock moved, since stock moves only at completion |

---

# Part 3 — Reflections & Lessons Learned (3 min)

## Slide 10 — Challenges and how we solved them (1.5 min)

1. **Throughput dropped 30–45 % after the service split.**
   - Investigation (spec 006) showed the **code was not the cause**: natively, the monolith and the services both ran about 800 tx/s on the stress workload. Under Docker Desktop the same code did about 409 tx/s.
   - About 44 % of the gap was port forwarding and about 52 % was VM and extra-hop overhead. nginx and the analytics pipeline cost roughly nothing.
2. **The hot-SKU lock turned latency into lost throughput.**
   - 67.7 % of baskets contain `SKU-000001`, so completions serialize on its row lock. Throughput ≈ 0.677 / (lock hold time), and the lock was held across 3–4 client round trips.
   - **Fix:** collapse the critical section into one statement, so only `COMMIT` waits while locks are held. Use autocommit for start and scan. Switch uvicorn to `httptools`.
3. **Analytics must not skip or double-count scans** when commits arrive out of order. Solved with the contiguous watermark and bounded gap-skipping.
4. **Keeping a shared database from becoming a ball of mud.** Solved with the ownership registry and `check_boundaries.py` in the test suite.

**Lesson:** measure before optimizing. The suspected culprit was innocent, and the real lever was fewer round trips under a lock.

**Show:** the TL;DR table in `specs/006-service-perf-investigation/investigation.md`.

## Slide 11 — Results (0.5 min)

| Run | Week 5 | Week 6 (spec 006) | Change |
| --- | --- | --- | --- |
| Default (10 stations, 60 s): tx/s | 220.6 | 262.5 | +19 % |
| Default: items/s | 4 250 | 5 615 | +32 % |
| Stress (100 stations, 120 s): tx/s | 190.9 | 224.5 | +18 % |
| Stress: items/s | 5 242 | 6 804 | +30 % |
| Stress `COMPLETE` p95 | 225 ms | 121.6 ms | −46 % |
| Invariant | PASS | PASS | — |

- Every error is a contract-defined `409 INSUFFICIENT_STOCK` once hot SKUs sell out. The rate rises with throughput because stock runs out sooner.
- Caveat: the Week 6 run was on battery power, so compare with the same-session numbers in the investigation.

## Slide 12 — Future improvements (1 min)

- **Reduce hot-SKU contention:** split or shard stock rows, or decrement through a queue, so the most popular SKU stops serializing completions.
- **Replace polling in Analytics** with change-data-capture or logical replication. Today's polling is simple and decoupled (the 005 review rejected `LISTEN/NOTIFY`), but it adds latency and idle queries.
- **Version the shared schema** with proper migrations, so a `checkout-data` bump does not require redeploying all readers at once.
- **Observability:** metrics for pipeline lag (`max_scan_seq − last_id`), skipped gaps and lock wait time, rather than logs only.
- **Run the real load test off Docker Desktop** (a Linux host) to remove the virtualization overhead from the measurements.

---

# Backup slides (use for Q&A or if time allows)

## Slide 13 — Boundary enforcement and testing **[OPTIONAL]** (1 min)

- `scripts/check_boundaries.py` runs in the test suite. It fails on cross-service imports, network clients in a service, SQL outside `checkout_data`, a repo writing a table it doesn't own, and compose environment variables naming another service's host.
- `scripts/check_independence.sh` stops and restarts each service and checks that the others keep working.
- Tests: `tests/unit` (includes `test_gateway_routes.py`, which asserts the test dispatcher's route table matches `gateway/nginx.conf`), `tests/integration` (real PostgreSQL), `tests/contract` (in-process via `tests/_dispatch.py`, or real HTTP through the gateway).
- The in-process suite uses its own `checkout_test` database and refuses to start unless the name ends in `_test`.
- Graded check: `docker compose run --rm tools python -m checkout_data.verify_invariant`.

## Slide 14 — Gateway details **[OPTIONAL]** (1 min)

- Routing only: no validation, rewriting, caching or DB access.
- Keepalive upstreams with `resolve` (Docker DNS, 5 s), so a restarted container is picked up automatically.
- `error_page 502 503 504 = @..._down` returns a contract-shaped JSON `503`.
- `proxy_set_header Upgrade ""` clears `Upgrade: h2c` so the services can run `httptools`.

**Show:** `backend/gateway/nginx.conf`.

## Slide 15 — Demo commands, Q&A, file map

**Demo (if requested):**

```bash
docker compose up -d --build --wait                 # build, seed, start, block until healthy
curl -s localhost:8080/items | head -c 120
docker compose stop analytics                       # only /analytics/popular-items returns a JSON 503
curl -s localhost:8080/analytics/popular-items
docker compose start analytics                      # window rebuilt from the database
./scripts/run_load_tests.sh                         # default and stress runs, with invariant check
```

**Likely questions:**
- *Why not have Checkout notify Analytics?* It adds a service-to-service dependency and a failure mode. Tailing the table keeps them independent and lets Analytics catch up after downtime.
- *What if the snapshot is lost or stale?* It is a derived cache. The pipeline rebuilds it from `transaction_item`, and the read path can compute on demand.
- *Why a window by scan id rather than by time?* It matches the spec (the most recent N scans), is deterministic, and is trivial to recompute from the table.
- *Why advisory locks instead of a leader-election service?* The database is already the one shared dependency.
- *Does Checkout scale out?* Yes. The locks live in the database, so replicas stay correct.
- *What stops SQL leaking into a service?* `check_boundaries.py` fails the test suite.

**File map:**

```
gateway/nginx.conf                                routing
packages/checkout_data/.../ownership.py           table ownership registry
packages/checkout_data/.../repos/checkout.py      Checkout SQL (scan CTE, completion statement)
packages/checkout_data/.../repos/analytics.py     tail query, window counts, snapshot upsert
packages/checkout_web/.../errors.py               contract error mapping
services/checkout/.../service.py                  business logic and transaction boundary
services/analytics/.../pipeline.py                five-stage streaming pipeline
services/analytics/.../supervisor.py              advisory-lock leader and restart logic
services/analytics/.../popular_items.py           read path and on-demand fallback
scripts/check_boundaries.py                       boundary enforcement
specs/006-service-perf-investigation/             performance investigation
```

---

## Suggested cuts

- **To 15 minutes:** drop slide 12's last two bullets and trim slide 9 to the five most important rows of the edge-case table.
- **To 12 minutes:** also show only `locked` and `ok` on slide 5, and merge slides 8 and 9 into one.
- **Never cut:** slides 2, 6 and 7. They are the guide's required case.
