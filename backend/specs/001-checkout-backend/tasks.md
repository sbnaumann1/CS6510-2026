---

description: "Task list for 001-checkout-backend"
---

# Tasks: Supermarket Self-Checkout Backend API

**Input**: Design documents from `/specs/001-checkout-backend/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/README.md)

**Tests**: Test tasks ARE included. `plan.md` Technical Context specifies pytest + pytest-asyncio + httpx contract and integration tests, and `contracts/README.md` states that `tests/contract/` "must pass before any load run is treated as valid."

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

Single project rooted at `backend/` per plan.md Structure Decision: `backend/app/`, `backend/scripts/`, `backend/tests/`. All paths below are repository-relative.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Host provisioning and project initialization

- [X] T001 Provision PostgreSQL 17 per quickstart.md: `brew install postgresql@17`, `brew services start postgresql@17`, `createdb checkout`, then apply the R11 tuning block (`shared_buffers = 4GB`, `work_mem = 32MB`, `max_connections = 200`, `synchronous_commit = off`, `wal_compression = on`, `checkpoint_timeout = 15min`, `max_wal_size = 8GB`) to `$(brew --prefix)/var/postgresql@17/postgresql.conf` and restart. Verify with `psql -d checkout -c 'select version();'` — PostgreSQL is NOT currently installed on this host (research.md Host baseline)
- [X] T002 Add missing dependencies to `backend/pyproject.toml` — `asyncpg` (research R1) and `orjson` (R7) to `[project.dependencies]`, plus `pytest`, `pytest-asyncio`, `httpx` as dev dependencies; retain `psycopg2-binary` for the sync seed/verify scripts only; run `uv sync`
- [X] T003 Create the package skeleton from plan.md Source Code layout: `backend/app/__init__.py`, `backend/app/api/__init__.py`, `backend/app/services/__init__.py`, `backend/scripts/`, `backend/tests/contract/`, `backend/tests/integration/`, `backend/tests/unit/`; delete the placeholder `backend/main.py`
- [X] T004 [P] Create `backend/app/config.py` with env-backed settings and exactly the defaults in the data-model.md Configuration table: `DATABASE_URL` (`postgresql+asyncpg:///checkout?host=/tmp`), `CATALOG_SIZE` (2000), `STOCK_PER_ITEM` (10000), `LOW_STOCK_THRESHOLD` (50), `POPULAR_WINDOW_SIZE` (1000), `POPULAR_SLIDE_INTERVAL` (500), `TX_ABANDON_MINUTES` (5), `DB_POOL_SIZE` (12), `WORKERS` (4)
- [X] T005 [P] Create `backend/.env.example` mirroring every setting in `backend/app/config.py` with its default value and a comment naming the consumer
- [X] T006 [P] Create `backend/docker-compose.yml` as the fallback PostgreSQL 17 service (research R3 — native Homebrew is preferred; this is used only when Homebrew is unavailable)
- [X] T007 [P] Create `backend/scripts/run_server.sh` (executable) launching uvicorn on port 8080 with `--workers ${WORKERS:-4}`, `--loop uvloop`, `--http httptools`, access log disabled, and HTTP keep-alive left enabled (contracts/README.md — the Java client reuses connections)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schema, error contract, catalog cache, and app wiring that every user story depends on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T008 Create all six SQLAlchemy ORM models in `backend/app/models.py` as the schema source of truth, reproducing the data-model.md DDL exactly: `catalog_item` (`sku TEXT PRIMARY KEY`, `name TEXT NOT NULL`, `price_cents INTEGER NOT NULL CHECK (price_cents > 0)`); `inventory_stock` (`sku TEXT PRIMARY KEY REFERENCES catalog_item(sku)`, `current_stock INTEGER NOT NULL CHECK (current_stock >= 0)`, `initial_stock INTEGER NOT NULL`, no `version` column); `transaction` (`id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`, `station_id TEXT NOT NULL`, `status tx_status NOT NULL DEFAULT 'OPEN'` over `CREATE TYPE tx_status AS ENUM ('OPEN','COMPLETED','CANCELLED')`, `item_count INTEGER NOT NULL DEFAULT 0`, `running_total_cents BIGINT NOT NULL DEFAULT 0`, `total_amount_cents BIGINT` nullable, `started_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `completed_at TIMESTAMPTZ` nullable) plus `CREATE INDEX transaction_open_started_idx ON transaction (started_at) WHERE status = 'OPEN'`; `transaction_item` (`id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`, `transaction_id BIGINT NOT NULL REFERENCES transaction(id)`, `sku TEXT NOT NULL REFERENCES catalog_item(sku)`, `unit_price_cents INTEGER NOT NULL`, `scanned_at TIMESTAMPTZ NOT NULL DEFAULT now()`) plus indexes `transaction_item_tx_idx (transaction_id)` and `transaction_item_sku_idx (sku)`; `low_stock_alert` (identity PK, `sku TEXT NOT NULL REFERENCES catalog_item(sku)`, `current_stock INTEGER NOT NULL`, `threshold INTEGER NOT NULL`, `triggered_at TIMESTAMPTZ NOT NULL DEFAULT now()`) plus `low_stock_alert_sku_time_idx (sku, triggered_at DESC)`; `popular_window_snapshot` (`id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1)`, `window_size INTEGER NOT NULL`, `slide_interval INTEGER NOT NULL`, `window_start BIGINT NOT NULL`, `window_end BIGINT NOT NULL`, `computed_at TIMESTAMPTZ NOT NULL`, `ranking JSONB NOT NULL`)
- [X] T009 [P] Create `backend/app/errors.py` with an `ApiError` exception carrying `(code, message, http_status)`, the seven codes from contracts/error-catalog.md (`INVALID_REQUEST` 400, `TRANSACTION_NOT_FOUND` 404, `SKU_NOT_FOUND` 404, `TRANSACTION_NOT_OPEN` 409, `EMPTY_BASKET` 409, `INSUFFICIENT_STOCK` 409, `INTERNAL_ERROR` 500), and handlers that render the body as exactly two string fields `{"error": ..., "message": ...}` with no other top-level keys
- [X] T010 [P] Create `backend/app/db.py` with the SQLAlchemy 2.0 async engine over asyncpg using `DATABASE_URL`, pool sized to `DB_POOL_SIZE` (keep `workers × pool < max_connections`), an async session factory, and a `pg_try_advisory_lock` helper for the analytics recompute (research R6)
- [X] T011 [P] Create `backend/app/schemas.py` with Pydantic request models (`StartTransactionRequest.stationId`, `ScanRequest.sku`) and response models for docs and cold paths, matching `contracts/openapi.yaml` field names exactly
- [X] T012 Create `backend/app/catalog_cache.py` loading `dict[sku] -> (name, price_cents)` at startup plus a pre-rendered `/items` JSON body (research R7), built in ascending SKU order — catalog response order defines the load client's Zipf rank (R12)
- [X] T013 Create `backend/scripts/seed.py` with an idempotent `--reset` that recreates the schema, loads `CATALOG_SIZE` items using the mock's exact formulas (`sku = 'SKU-%06d' % i`, `name = 'Item %d' % i`, `price = round(0.5 + (i % 47) * 0.35, 2)` converted to `price_cents`), sets both `current_stock` and `initial_stock` to `STOCK_PER_ITEM`, and clears transactions, line items, alerts and the window snapshot
- [X] T014 Create `backend/app/api/catalog.py` serving `GET /items` from the pre-rendered cache body in ascending SKU order, returning 200 with `items[].sku`, `items[].name`, `items[].price` (FR-010)
- [X] T015 Create the app factory in `backend/app/main.py` — `ORJSONResponse` as the default response class (R7), lifespan hook loading the catalog cache, registration of the `ApiError`/`RequestValidationError`/`HTTPException`/catch-all handlers from T009 so validation failures return `400 {"error":"INVALID_REQUEST",...}` instead of FastAPI's `422 {"detail":[...]}`, and router wiring
- [X] T016 Create `backend/tests/conftest.py` with pytest-asyncio configuration, an httpx `AsyncClient` fixture against the app, and a per-test database reset fixture reusing `scripts/seed.py`
- [X] T017 Create the contract test for `GET /items` in `backend/tests/contract/test_catalog.py` asserting 200, the exact field names `sku`/`name`/`price`, JSON types, `CATALOG_SIZE` items, and strictly ascending SKU order

**Checkpoint**: Schema, error contract, catalog and app factory ready — user story implementation can begin

---

## Phase 3: User Story 1 - Customer Completes Checkout Transaction (Priority: P1) 🎯 MVP

**Goal**: A customer can start a transaction, scan 1–20 units, and complete it — with inventory decremented exactly once, at completion only, and never below zero.

**Independent Test**: Run quickstart V2 (start → 3 scans → complete → post-completion scan rejected), then `scripts/verify_invariant.py`: transaction ID and status `OPEN` on start, `itemCount` climbing with matching `runningTotal`, a receipt whose lines collapse duplicate SKUs to `quantity: 2` and whose `totalAmount` equals the sum of the lines, `409` on the post-completion scan, and SKU-000001 stock down by exactly 2.

### Tests for User Story 1

> Write these first and confirm they fail before implementing T024–T029

- [X] T018 [P] [US1] Contract test for `POST /transactions` in `backend/tests/contract/test_transactions_start.py` — asserts **201** (not 200), body fields `transactionId`/`stationId`/`status`, `status == "OPEN"`, `transactionId` matching `tx-<digits>`, and `400 INVALID_REQUEST` when `stationId` is missing, empty, or not a string
- [X] T019 [P] [US1] Contract test for `POST /transactions/{id}/items` in `backend/tests/contract/test_transactions_scan.py` — asserts 200 with fields `sku`/`unitPrice`/`itemCount`/`runningTotal`, plus the error paths `400 INVALID_REQUEST` (missing/non-string `sku`), `404 TRANSACTION_NOT_FOUND` (unknown ID and malformed non-`tx-<digits>` ID), `404 SKU_NOT_FOUND`, `409 TRANSACTION_NOT_OPEN`
- [X] T020 [P] [US1] Contract test for `POST /transactions/{id}/complete` in `backend/tests/contract/test_transactions_complete.py` — asserts 200 with `itemCount`/`totalAmount`, that a `{}` body with `Content-Type: application/json` is accepted and a body is not required, INV-5 (`totalAmount == Σ line unitPrice × quantity`), and the error paths `404 TRANSACTION_NOT_FOUND`, `409 TRANSACTION_NOT_OPEN`, `409 EMPTY_BASKET`, `409 INSUFFICIENT_STOCK`
- [X] T021 [P] [US1] Contract test for `GET /transactions/{id}` in `backend/tests/contract/test_transactions_get.py` — asserts 200 with the transaction shape and `404 TRANSACTION_NOT_FOUND`
- [X] T022 [P] [US1] Integration test for the full checkout journey in `backend/tests/integration/test_checkout_flow.py` covering spec.md US1 acceptance scenarios 1–4, and asserting INV-4 (`transaction.item_count == COUNT(transaction_item)` and `running_total_cents == SUM(unit_price_cents)`) after a scripted basket
- [X] T023 [P] [US1] Integration test for concurrency in `backend/tests/integration/test_concurrent_completion.py` — INV-3 (two concurrent completes of the same transaction decrement stock at most once), the last-unit race (two stations completing against stock of 1: exactly one succeeds, the other gets `409 INSUFFICIENT_STOCK`), and INV-6 (no decrement for OPEN or CANCELLED transactions)

### Implementation for User Story 1

- [X] T024 [US1] Implement `start_transaction` in `backend/app/services/transactions.py` — insert with `status='OPEN'`, return public ID `'tx-' || id` (research R5)
- [X] T025 [US1] Implement the scan path in `backend/app/services/transactions.py` per research R9 — resolve SKU from the in-memory catalog cache and raise `SKU_NOT_FOUND` before any DB work, then one CTE statement that updates `item_count`/`running_total_cents` on the `OPEN` transaction, inserts the `transaction_item` row (one row per scanned unit), and `RETURNING`s `item_count`/`running_total_cents`; zero rows back triggers one cheap follow-up query to choose between `404` and `409`
- [X] T026 [US1] Implement the completion path in `backend/app/services/transactions.py` per research R10 steps 1–4, 6, 7 in one explicit DB transaction: `SELECT ... FOR UPDATE` the transaction row (404 absent / 409 not OPEN); `GROUP BY sku ORDER BY sku` the line items (409 `EMPTY_BASKET` if none); `SELECT sku FROM inventory_stock WHERE sku = ANY(:skus) ORDER BY sku FOR UPDATE` to take locks in deterministic SKU order; one conditional batched `UPDATE ... FROM (VALUES ...) WHERE s.sku = v.sku AND s.current_stock >= v.qty RETURNING s.sku, s.current_stock` — if fewer rows return than distinct SKUs, roll back the whole transaction and respond `409 INSUFFICIENT_STOCK` leaving the transaction `OPEN`; then mark `COMPLETED` with `completed_at` and `total_amount_cents`; build the receipt from the grouped lines plus the catalog cache. Stock must never be clamped
- [X] T027 [US1] Create `backend/app/api/transactions.py` with the four routes — `POST /transactions` decorated `status_code=201`, `POST /{id}/items`, `POST /{id}/complete` (optional body), `GET /{id}` — converting integer cents to the contract's decimal money shape at the JSON boundary only (R4/INV-5)
- [X] T028 [US1] Enforce the contracts/error-catalog.md precedence order in `backend/app/api/transactions.py` and `backend/app/services/transactions.py`: `INVALID_REQUEST` → `TRANSACTION_NOT_FOUND` → `SKU_NOT_FOUND` → `TRANSACTION_NOT_OPEN` → `EMPTY_BASKET` → `INSUFFICIENT_STOCK`, with `tx-<digits>` ID validation rejecting malformed IDs as `404 TRANSACTION_NOT_FOUND` without a query
- [X] T029 [US1] Create `backend/scripts/verify_invariant.py` (the graded check, SC-003/SC-004) asserting per SKU that `initial_stock - current_stock == COUNT(transaction_item rows in COMPLETED transactions)` and `current_stock >= 0`, printing a pass/fail summary plus the worst offenders

**Checkpoint**: US1 is fully functional — the load client can run a complete checkout workload against it

---

## Phase 4: User Story 2 - Inventory Management & Low-Stock Alerts (Priority: P2)

**Goal**: Stock levels are queryable, low-stock alerts are raised on threshold crossings, and overselling is impossible.

**Independent Test**: Seed an item to 100 units, complete 60 single-item transactions, query `/inventory/low-stock?threshold=50`, and verify the item appears with `currentStock: 40`; then run quickstart V3 and confirm `threshold` echoes the override, `generatedAt` is present, and `alerts[]` is in timestamp-then-SKU order.

### Tests for User Story 2

- [X] T030 [P] [US2] Contract test for `GET /inventory/low-stock` in `backend/tests/contract/test_inventory.py` — asserts 200 with `alerts[].sku`/`.name`/`.currentStock`/`.threshold`, top-level `threshold` echoing the `?threshold=` override or the configured default, `generatedAt` present, and `400 INVALID_REQUEST` when `threshold` is not a positive integer
- [X] T031 [P] [US2] Integration test for alert crossings in `backend/tests/integration/test_low_stock.py` — one alert row per descent (crossing condition `current_stock < threshold <= current_stock + qty`), not one per completion; the alert row is committed atomically with the decrement that caused it (SC-005); and spec.md US2 acceptance scenarios 1–3

### Implementation for User Story 2

- [X] T032 [P] [US2] Implement the low-stock query in `backend/app/services/inventory.py` using the data-model.md statement — live `inventory_stock` joined to `catalog_item`, `LEFT JOIN LATERAL` the most recent `low_stock_alert` per SKU, `WHERE s.current_stock < :threshold`, `ORDER BY a.triggered_at NULLS LAST, c.sku` (FR-007 timestamp order, deterministic for SKUs with no alert row); echo the effective threshold and set `generatedAt` to `now()`; no pagination
- [X] T033 [US2] Create `backend/app/api/inventory.py` with `GET /inventory/low-stock`, validating the optional `?threshold=` as a positive integer and defaulting to `LOW_STOCK_THRESHOLD`
- [X] T034 [US2] Add research R10 step 5 to the completion transaction in `backend/app/services/transactions.py` — insert `low_stock_alert` rows for any SKU returned by the batched decrement that crossed the configured `LOW_STOCK_THRESHOLD` on this transaction (`current_stock < threshold <= current_stock + qty`), inside the same transaction as the decrement (FR-006)

**Checkpoint**: US1 and US2 both work; stock is queryable and alerts are durable

---

## Phase 5: User Story 3 - Popular Items Analytics with Sliding Window (Priority: P3)

**Goal**: A hopping window over the most recent 1000 scans, recomputed every 500 scans, serving identical rankings from every worker.

**Independent Test**: Scan a skewed distribution (SKU-000001 ×150, SKU-000002 ×100, …) past 1000 scans, query `/analytics/popular-items?limit=10`, and verify the top result is SKU-000001 with the true `scanCount`, contiguous `rank` starting at 1, and `windowStart`/`windowEnd` bracketing at most 1000 sequence numbers.

### Tests for User Story 3

- [X] T035 [P] [US3] Contract test for `GET /analytics/popular-items` in `backend/tests/contract/test_analytics.py` — asserts 200 with `items[].sku`/`.name`/`.scanCount`/`.rank` plus window metadata `windowSize` (1000), `slideInterval` (500), `windowStart`, `windowEnd`, `computedAt`; `?limit=` honored; `400 INVALID_REQUEST` when `limit` is not a positive integer
- [X] T036 [P] [US3] Integration test for window mechanics in `backend/tests/integration/test_popular_items.py` — ranking reflects true scan counts within the 500-scan slide interval (SC-006), the fewer-than-`windowSize` case returns the available items with `windowStart: 0`, and a `limit` larger than the available item count returns all of them (spec.md US3 acceptance scenarios 1–2 and the window-underflow edge case)

### Implementation for User Story 3

- [X] T037 [US3] Implement the window recompute and read in `backend/app/services/analytics.py` per research R6 — recompute runs `SELECT sku, count(*) FROM transaction_item WHERE id > :window_end - :window_size AND id <= :window_end GROUP BY sku ORDER BY count(*) DESC, sku LIMIT 200` and upserts `ranking` plus `window_start`/`window_end`/`computed_at` into the single `popular_window_snapshot` row via `INSERT ... ON CONFLICT (id) DO UPDATE`; the read path is one PK fetch sliced to `limit`, attaching `name` and `rank` (index + 1) from the in-memory catalog cache; when no snapshot row exists yet, compute on demand over the scans that exist
- [X] T038 [US3] Create `backend/app/background.py` with the recompute trigger that takes `pg_try_advisory_lock` so exactly one worker recomputes per slide, and skips without blocking when the lock is held
- [X] T039 [P] [US3] Create `backend/app/api/analytics.py` with `GET /analytics/popular-items`, validating `?limit=` as a positive integer with a default of 10
- [X] T040 [US3] Fire the background recompute from the scan path in `backend/app/services/transactions.py` when the returned `transaction_item.id` crosses a multiple of `POPULAR_SLIDE_INTERVAL`, off the request's critical path

**Checkpoint**: All three user stories are independently functional

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T041 [P] Unit tests for money math in `backend/tests/unit/test_money.py` — integer-cent arithmetic, the single conversion at the JSON boundary, and that a 20-line basket total never drifts (R4)
- [X] T042 [P] Unit tests for settings defaults in `backend/tests/unit/test_config.py` against the data-model.md Configuration table
- [X] T043 Add the abandoned-transaction sweeper to `backend/app/background.py` — transition `OPEN` transactions older than `TX_ABANDON_MINUTES` (default 5) to `CANCELLED` using the `transaction_open_started_idx` partial index, decrementing no stock (spec.md Edge Cases, INV-6)
- [X] T044 [P] Write `backend/README.md` covering provisioning, seeding, running, and where the design artifacts live
- [X] T045 Run the V1–V5 scenarios in `specs/001-checkout-backend/quickstart.md` — `uv run pytest -q` over `backend/tests/` plus the scripted checkout, alerts/analytics, invariant and restart smoke checks — and confirm all pass
- [X] T046 Run quickstart V6 — `scripts/seed.py --reset`, then the load client at `--stations=10 --duration=60`, then `verify_invariant.py`; record p50/p95/p99 and throughput (SC-001, SC-002) and note the stock-depletion point from the low-stock endpoint (research R13)
- [X] T047 Run the V7 companion scenario in `specs/001-checkout-backend/quickstart.md` — `STOCK_PER_ITEM=200000 uv run python scripts/seed.py --reset` then the load client at `--stations=10 --duration=60`; demonstrates a 0% error rate without clamping stock. Save the report to `load-client/reports/` and label it alongside V6
- [X] T048 Run quickstart V8 — the assignment's stress configuration `--stations=100 --duration=120`, then `verify_invariant.py`; the correctness invariant is the pass/fail criterion (SC-008)
- [X] T049 Run the V9 durability scenario in `specs/001-checkout-backend/quickstart.md` — restart via `backend/scripts/run_server.sh` and confirm completed transactions, stock levels and alerts survive, with `backend/scripts/verify_invariant.py` still passing (SC-004)
- [X] T050 Tune `WORKERS` and `DB_POOL_SIZE` against the plan.md target band (p50 ≤ 1.5 ms, p95 ≤ 4 ms, p99 ≤ 10 ms, ≥ 400 tx/s), keeping `workers × pool < max_connections`; record the final values in `backend/.env.example` and update plan.md Performance Goals with the measured numbers

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: T001 (PostgreSQL) blocks everything that touches a database; T002–T007 can proceed alongside it
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **User Stories (Phase 3–5)**: All depend on Foundational completion
- **Polish (Phase 6)**: T045–T050 depend on all three stories being complete

### User Story Dependencies

- **US1 (P1)**: Depends only on Foundational. No dependency on US2 or US3 — this is the MVP
- **US2 (P2)**: T030–T033 are independent of US1. **T034 edits the completion transaction created by T026**, so it must follow US1's T026. This is the one deliberate cross-story dependency: FR-006 requires the alert to be written atomically with the decrement, so it cannot live outside the completion path
- **US3 (P3)**: T035–T039 are independent of US1 and US2. **T040 edits the scan path created by T025**, so it must follow US1's T025

### Within Each User Story

- Tests are written and confirmed failing before implementation
- Models → services → endpoints → integration
- In US1, T024 → T025 → T026 are sequential (same file, and completion reads what scan wrote)

### Parallel Opportunities

- Setup: T004, T005, T006, T007 in parallel after T003
- Foundational: T009, T010, T011 in parallel after T008
- US1: all six test tasks T018–T023 in parallel; implementation T024–T026 is sequential (one file)
- US2: T030, T031 in parallel; T032 parallel with them
- US3: T035, T036 in parallel; T039 parallel with T037
- Polish: T041, T042, T044 in parallel
- With multiple developers, US2's T030–T033 and US3's T035–T039 can proceed while US1 is still in flight; only T034 and T040 need US1 landed

---

## Parallel Example: User Story 1

```bash
# Launch all six US1 test tasks together:
Task: "Contract test for POST /transactions in backend/tests/contract/test_transactions_start.py"
Task: "Contract test for POST /transactions/{id}/items in backend/tests/contract/test_transactions_scan.py"
Task: "Contract test for POST /transactions/{id}/complete in backend/tests/contract/test_transactions_complete.py"
Task: "Contract test for GET /transactions/{id} in backend/tests/contract/test_transactions_get.py"
Task: "Integration test for the full checkout journey in backend/tests/integration/test_checkout_flow.py"
Task: "Integration test for concurrency in backend/tests/integration/test_concurrent_completion.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 Setup — provision PostgreSQL first; it is the long pole and is not yet installed
2. Phase 2 Foundational — schema, error contract, catalog cache, app factory
3. Phase 3 US1 — start, scan, complete, plus `verify_invariant.py`
4. **STOP and VALIDATE**: quickstart V2 and V4; then a short load-client run. US1 alone exercises every endpoint the client calls in its hot loop, so the MVP is already measurable

### Incremental Delivery

1. Setup + Foundational → `GET /items` serves the catalog
2. Add US1 → a full checkout works and the invariant holds → measurable with the load client
3. Add US2 → alerts and low-stock queries → the client's end-of-run alert fetch returns real data
4. Add US3 → popular-items window → the client's end-of-run analytics fetch returns real rankings
5. Polish → sweeper, tuning, and the four recorded runs (V6–V9) that form the submission

### Notes

- [P] tasks touch different files and have no incomplete dependencies
- The contract is frozen: `contracts/openapi.yaml` and `backend/spec.yaml` are never edited to fit the implementation
- Re-run `scripts/seed.py --reset` before every measured run — a run against depleted stock is not comparable
- Commit after each task or logical group
