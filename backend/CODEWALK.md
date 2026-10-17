# Codewalk Outline — Self-Checkout Backend

Slide-by-slide outline for a 10–15 minute codewalk. Each slide lists the **talking points**,
the **code to show** (`path:line`), and a **time budget**. Slides marked **[OPTIONAL]** are
the first to cut. The core path (unmarked) totals about 13 minutes.

| # | Slide | Min | Status |
| --- | --- | --- | --- |
| 1 | Title and the problem | 0.5 | core |
| 2 | Architecture at a glance | 1.5 | core |
| 3 | Gateway: routing only | 1 | core |
| 4 | Shared database, enforced ownership | 1.5 | core |
| 5 | Shared packages | 1 | optional |
| 6 | Request walkthrough: start and scan | 1.5 | core |
| 7 | Completion: the critical section | 2.5 | core |
| 8 | Analytics: the streaming pipeline | 2 | core |
| 9 | Supervisor: one leader, restart-safe | 1 | optional |
| 10 | Independence and testing | 1 | core |
| 11 | Performance: findings and the spec 006 fix | 1.5 | core |
| 12 | Results | 1 | core |
| 13 | Lessons and trade-offs | 0.5 | core |
| 14 | Appendix: demo commands, Q&A backup | — | backup |

---

## Slide 1 — Title and the problem (0.5 min)

**Title:** Self-Checkout Backend — Domain Services over a Shared Database

- One frozen API contract (`spec.yaml`, OpenAPI 3.0.3). Only the server architecture changes week to week, and the same Java load client runs unmodified every time.
- Domain: stations start a transaction, scan items one at a time, then complete to pay.
- Stock is decremented **at completion**, never at scan.
- Graded correctness property: stock never goes negative, and `initial_stock − current_stock` equals the sum of completed decrements.
- Week 5 goal: split the monolith into independently deployable services by business function.

**Show:** the summary line of `spec.yaml` (domain model section, lines ~10–45).

---

## Slide 2 — Architecture at a glance (1.5 min)

- Four domain services, one PostgreSQL 17 database, one nginx gateway on `:8080`.
- **Services never call each other.** Their only connection is the shared database.

| Service | Owns | Endpoints |
| --- | --- | --- |
| Catalog | what can be sold | `GET /items` |
| Checkout | basket start to payment | `POST /transactions`, `/transactions/{id}/items`, `/transactions/{id}/complete`, `GET /transactions/{id}` |
| Inventory | stock that needs attention | `GET /inventory/low-stock` |
| Analytics | buying behaviour | `GET /analytics/popular-items` |

- Also in the stack: `seed` (one-shot, seeds an empty DB) and `tools` (on-demand invariant check).
- Startup order: postgres healthy → seed completes → services healthy → gateway.

**Show:** the *Infrastructure* mermaid diagram in `backend/README.md` (thick arrows = writes, dotted = reads). Alternatively, show `compose.yaml` with the `depends_on` chain.

**Speaker note:** point out that there are no arrows between the services.

---

## Slide 3 — Gateway: routing only (1 min)

- nginx routes by path, so the client sees a single base URL.
- No validation, rewriting, caching or database access.
- If a service is down, **only its routes fail**, and they fail as a contract-shaped `503` JSON body.
- Keepalive upstreams and `resolve` (Docker DNS, re-resolved every 5 s) mean a restarted container is picked up automatically.

**Show:** `backend/gateway/nginx.conf`
- upstream blocks (`zone`, `keepalive 64`, `resolve`)
- `location = /items`, `location /transactions`, and the `error_page 502 503 504 = @..._down` fallbacks
- the explicit `proxy_set_header Upgrade ""` (needed so services can run `httptools`; revisit on slide 11)

---

## Slide 4 — Shared database, enforced ownership (1.5 min)

- Coupling through a database is risky, so the coupling is **declared and machine-checked**.
- `ownership.TABLES` names one writing owner and an explicit reader set for every table.

| Table | Owner | Readers |
| --- | --- | --- |
| `catalog_item` | catalog (seed only) | all four |
| `inventory_stock` | checkout | checkout, inventory |
| `transaction` | checkout | checkout |
| `transaction_item` | checkout | checkout, analytics |
| `low_stock_alert` | checkout | inventory |
| `popular_window_snapshot` | analytics | analytics |

- `scripts/check_boundaries.py` runs in the test suite and fails on:
  - cross-service imports;
  - network clients inside a service;
  - SQL outside `checkout_data`;
  - writing a table you don't own, or reading one you aren't a reader of;
  - compose environment variables that name another service's host.
- Business functions communicate through data: Inventory reports from rows Checkout wrote, and Analytics tails the scans Checkout inserted. Nothing notifies either of them.

**Show:** `packages/checkout_data/src/checkout_data/ownership.py:28-36` (the `TABLES` dict), then a few lines of `scripts/check_boundaries.py`.

---

## Slide 5 — Shared packages **[OPTIONAL]** (1 min)

- `checkout_data` holds the schema (`models.py`), engine and sessions, advisory-lock helpers, seed and verify CLIs, and **one repository module per service** (`repos/catalog|checkout|inventory|analytics.py`). Every SQL statement in the system lives here.
- `checkout_web` holds the error shape, request parsing, the app factory and `/healthz`. It has no database access.
- Each image installs both as ordinary packages, with its own `requirements.lock`. A schema change means bumping `checkout-data` and redeploying that table's readers. That cost is accepted and visible.
- Layering inside a service: `api.py` (HTTP) → `service.py` (business rules and transaction boundary) → `repos/<service>.py` (one statement per function, never commits).

**Show:** `packages/checkout_web/src/checkout_web/app.py` (34 lines; `create_service_app`) and the *code-coupling* diagram in the README.

---

## Slide 6 — Request walkthrough: start and scan (1.5 min)

- A scan is the hot path: 1 SKU lookup in memory plus **1 SQL round trip**.
- Catalog is loaded into an in-process cache at startup (`checkout_data/catalog.py`), so pricing and `SKU_NOT_FOUND` need no DB.
  - Error precedence is preserved: `SKU_NOT_FOUND` comes before `TRANSACTION_NOT_OPEN`.
- One CTE does the whole scan: bump the transaction's counters, insert the line, and `RETURNING` both.
  - `WHERE status = 'OPEN'` means a closed transaction matches no row, so there is nothing to undo.
  - The rare failure path runs one cheap query to pick between 404 and 409.
- Start and scan run on an **autocommit session** (`get_autocommit_session`), so there is no `BEGIN`/`COMMIT` round trip.
- Money is integer cents end to end (`money.py`).
- `transaction_item.id` is the global **scan sequence**, which is the cursor Analytics tails.

**Show:**
- `services/checkout/src/checkout_service/api.py` (thin handler)
- `services/checkout/src/checkout_service/service.py:60-97` (`scan_item`)
- `packages/checkout_data/src/checkout_data/repos/checkout.py:27-45` (`_SCAN` CTE)

---

## Slide 7 — Completion: the critical section (2.5 min)

**The hard part, and the centerpiece of the talk.** Concurrency can break correctness here.

Flow in `complete_transaction` (`service.py:109-175`), inside one explicit DB transaction:
1. `lock_transaction` takes `SELECT … FOR UPDATE` on the transaction row. This serializes double-completes, so there is no double decrement.
2. `get_basket` collapses scans to `(sku, qty)` in SKU order.
3. `complete_basket` is **one SQL statement** that locks, checks, decrements, alerts and finalizes.
4. On `ok = false`, nothing was modified and the API returns `409 INSUFFICIENT_STOCK` with the short SKUs.
5. The receipt is built from the basket plus the in-memory catalog.

Why the single statement is correct (`repos/checkout.py:57-110`):
- **No deadlock:** the `locked` CTE takes every stock row `FOR UPDATE` in **SKU order**, so lock acquisition order is globally consistent.
- **No partial updates:** `locked` is `MATERIALIZED` and read by `ok`. `dec` and `fin` run only when `ok`, so all locks are granted before anything is modified.
- **Never oversell:** under READ COMMITTED, the lock and the `UPDATE` both see the latest committed stock, so the check and the decrement agree. `CHECK (current_stock >= 0)` is a backstop.
- **Alert exactly once per descent:** `current_stock < :thr AND :thr <= current_stock + qty` fires only on the crossing, and the alert row is written in the same transaction as the decrement.
- **One statement text for every basket size:** the SKUs and quantities are passed as arrays (`unnest`), not as a `VALUES` list.

**Show:** the `_COMPLETE_BASKET` SQL side by side with the `complete_transaction` flow. Highlight `locked`, `ok`, `dec`, `alerts` and `fin`.

**Speaker note:** the CTE chain reads top to bottom as lock → check → decrement → alert → finalize. If time is short, show only `locked` and `ok`.

---

## Slide 8 — Analytics: the streaming pipeline (2 min)

- One persistent pipeline for the service's whole life, with no new run every 500 scans.
- A **pipes-and-filters** design: asyncio tasks connected by queues.

```text
TailIngest ─q1─▶ WindowFilter ─q2─▶ AggregationFilter ─q3─▶ RankingFilter ─slot─▶ OutputFilter
 (DB tail)       (slide by id)      (running counts)        (top-200)             (latest-wins, commit)
```

| Stage | Job | Code |
| --- | --- | --- |
| `TailIngest` | Follows `transaction_item` as rows commit | `pipeline.py:94` |
| `WindowFilter` | 1 000-scan window, slides every 500, keyed by scan id | `pipeline.py:188` |
| `AggregationFilter` | Running per-SKU counts | `pipeline.py:217` |
| `RankingFilter` | Top 200, ties broken by SKU | `pipeline.py:236` |
| `LatestSlot` and `OutputFilter` | Latest-wins handoff, one snapshot row | `pipeline.py:263`, `pipeline.py:291` |

- **Why a contiguous watermark in `TailIngest`:** sequence ids can commit out of order, so the tail only advances over a gap-free prefix and scans are never skipped. An id that never commits (a rolled-back transaction) is skipped after 1 s (`_skip_expired_hole`, `pipeline.py:150`).
- **Why latest-wins at the end:** a slow DB write must not back up the pipeline, so only the newest ready window is written.
- The API (`api.py`, `popular_items.py`) just reads the single `popular_window_snapshot` row. Every worker and replica serves the same answer.

**Show:** the diagram above, `TailIngest._advance/_drain/_skip_expired_hole`, then `LatestSlot`.

---

## Slide 9 — Supervisor: one leader, restart-safe **[OPTIONAL]** (1 min)

- `PipelineSupervisor` (`supervisor.py:45`) holds a **PostgreSQL session-level advisory lock** (`POPULAR_RECOMPUTE_LOCK`), so only one pipeline runs across replicas. The others stand by and retry.
- On start it rebuilds from the database: it reads `max_scan_seq`, aligns to the slide boundary, and starts the window one window-size earlier. A restarted Analytics therefore catches up on scans made while it was down.
- If the pipeline throws, the supervisor logs, backs off and rebuilds. It always cancels filters and unlocks in `finally`.
- Advisory-lock helpers live in `checkout_data/__init__.py:70-92`.

**Show:** `supervisor.py:63-130` (`run`, `_acquire`, `_run_pipeline`).

---

## Slide 10 — Independence and testing (1 min)

- **Independence is tested, not assumed.** `scripts/check_independence.sh` stops and restarts each service in turn and checks that the others keep working.
- Test pyramid (`tests/`):
  - `unit/`: includes `test_gateway_routes.py`, which asserts the test dispatcher's route table matches `gateway/nginx.conf` exactly.
  - `integration/`: the services against a real PostgreSQL.
  - `contract/`: the same tests run in-process (via `tests/_dispatch.py`, which mirrors the gateway) or over real HTTP through the gateway and containers.
- The in-process suite uses its own `checkout_test` DB and refuses to start if the name doesn't end in `_test`.
- Graded correctness check: `docker compose run --rm tools python -m checkout_data.verify_invariant`.

**Show:** the `verify_invariant.py` output (all SKUs: initial = final + decremented → PASS).

---

## Slide 11 — Performance: findings and the spec 006 fix (1.5 min)

**Question:** why did Week 5 lose 30–45 % of throughput versus the Week 4 monolith?

- **Finding 1: it was not the code, it was the deployment.** Run natively, the monolith and the 005 services do about 800 tx/s on the stress workload. Under Docker Desktop the same code does about 409 tx/s.
  - About 44 % of the gap is Docker port forwarding, and about 52 % is VM and extra-hop overhead.
  - nginx and the analytics pipeline cost roughly nothing.
- **Finding 2: the hot-SKU lock turns latency into lost throughput.** 67.7 % of baskets contain `SKU-000001`, so completions serialize on that row lock. Throughput ≈ 0.677 / (lock hold time), and the lock was held across 3–4 client round trips.
- **Fix (spec 006), three changes:**
  1. Completion's critical section becomes one statement, so the locks wait on `COMMIT` only.
  2. Start and scan use autocommit sessions, one round trip each.
  3. uvicorn runs `httptools` instead of `h11`. The gateway clears `Upgrade: h2c` so this is safe.

**Show:** the TL;DR table in `specs/006-service-perf-investigation/investigation.md`, then `repos/checkout.py:57-69` (the design comment).

---

## Slide 12 — Results (1 min)

| Run | Week 5 | Week 6 (spec 006) | Change |
| --- | --- | --- | --- |
| Default (10 stations, 60 s): tx/s | 220.6 | 262.5 | +19 % |
| Default: items/s | 4 250 | 5 615 | +32 % |
| Stress (100 stations, 120 s): tx/s | 190.9 | 224.5 | +18 % |
| Stress: items/s | 5 242 | 6 804 | +30 % |
| Stress `COMPLETE` p95 | 225 ms | 121.6 ms | −46 % |
| Invariant | PASS | PASS | — |

- Every error is a contract-defined `409 INSUFFICIENT_STOCK` once hot SKUs sell out. The error rate rises with throughput because stock runs out sooner.
- Caveat: Week 6 ran on battery power, so compare with same-session numbers in the investigation, not across weeks.

**Show:** a bar chart of tx/s (Week 5 vs Week 6), built from `load-client/reports/README.md`.

---

## Slide 13 — Lessons and trade-offs (0.5 min)

- A shared database gives simple, strongly consistent stock, but at the price of schema coupling. Ownership rules plus a boundary check keep that coupling honest.
- Fewer round trips under a lock matters more than fast code. Measure before optimizing: the suspected culprit (the refactor) was innocent.
- Keep the contract frozen and the harness constant, so architecture comparisons mean something.
- Next steps (pick one): reduce lock contention on hot SKUs (for example, split or shard stock rows), or move Analytics to a change-data-capture feed instead of tailing.

---

## Slide 14 — Appendix and backup material

**Demo commands (if a live demo is requested):**

```bash
docker compose up -d --build --wait                 # build, seed, start, block until healthy
curl -s localhost:8080/items | head -c 120
docker compose stop analytics                       # only /analytics/popular-items returns a JSON 503
curl -s localhost:8080/analytics/popular-items
docker compose start analytics                      # window rebuilt from the database
./scripts/run_load_tests.sh                         # default run, then stress, with invariant check
```

**Likely questions and short answers:**
- *Why not have Checkout notify Analytics?* It would add service-to-service calls and a failure mode. Tailing the table keeps services independent and lets Analytics catch up after downtime.
- *Why advisory locks instead of leader election?* The database is already the one shared dependency, so no extra infrastructure is needed.
- *What stops SQL from leaking into a service?* `check_boundaries.py` fails the test suite.
- *Does it scale out?* Checkout replicas are correct because the locks live in the database. Analytics replicas stand by behind the advisory lock.
- *Abandoned transactions?* `sweeper.py` cancels `OPEN` transactions older than a threshold, using the `transaction_open_started_idx` partial index.

**Files at a glance:**

```
gateway/nginx.conf                                routing
packages/checkout_data/.../ownership.py           table ownership registry
packages/checkout_data/.../repos/checkout.py      all Checkout SQL (scan CTE, completion statement)
services/checkout/.../service.py                  business logic and transaction boundary
services/analytics/.../pipeline.py                five-stage streaming pipeline
services/analytics/.../supervisor.py              advisory-lock leader and restart logic
scripts/check_boundaries.py                       boundary enforcement
specs/006-service-perf-investigation/             performance investigation
```

---

## Suggested cuts to reach 10 minutes

Drop slides 5 and 9, shorten slide 3 to a single screenshot, and show only `locked` and `ok` on slide 7.
