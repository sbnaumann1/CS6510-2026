---
description: "Task list for 007 microservices architecture"
---

# Tasks: Microservices Architecture (Database per Service, Coordinated Cross-Service Updates)

**Input**: Design documents from `specs/007-microservices-architecture/`
**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: **Included.** The spec requires them: failure injection at every saga step (FR-023), the boundary check (FR-022), the cross-store invariant (FR-024), and the unchanged contract suite (SC-001).

**Paths**: Relative to `backend/` unless they start with `../` (the repo root, e.g. `../load-client/`). Python 3.11. Run commands from `backend/`.

**Ground rules for every task**:
- **Moving code**: when a task says "move" or "port", copy the 005 code and change only imports, table names and what the task names. Do not "improve" it. The 005 SQL statements are tuned (spec 006) and stay as they are.
- **No shared data code**: a service never imports another service's package, and `checkout_web` never imports SQLAlchemy ([contracts/service-boundaries.md](./contracts/service-boundaries.md)).
- **Per-service database URL**: each service's `config.py` reads `<OWN_NAME>_DATABASE_URL` first (for example `INVENTORY_DATABASE_URL`) and falls back to `DATABASE_URL`. Only the service's own name appears in its code. This lets four in-process test apps use four databases.
- **Contract tests**: assertions in `tests/contract/` are not edited. Fixtures may change.
- **Old code**: `packages/checkout_data/` stays in place as the reference until T048 deletes it. Do not import it from new code.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US5 from spec.md

---

## Phase 1: Setup (package skeletons)

**Purpose**: Each service becomes self-contained and installable, with no dependency on `checkout_data`.

- [ ] T001 Create the empty module files for every new service module listed in [plan.md](./plan.md) Project Structure under `services/*/src/*_service/` (`config.py`, `db.py`, `models.py`, `repo.py`, `seed.py`, plus `reserve.py`, `saga.py`, `recovery.py`, `scan_relay.py`, `*_client.py`, `ingest.py` where listed). Each file contains only a module docstring naming its purpose.
- [ ] T002 [P] Update the four service manifests `services/{catalog,checkout,inventory,analytics}/pyproject.toml`. Bump `version` to `0.7.0`. Replace the `checkout-data` dependency with `sqlalchemy[asyncio]==2.0.23`, `greenlet>=3.0.0`, `asyncpg==0.29.0`, `psycopg2-binary==2.9.9`, `python-dotenv==1.0.0`, and `httpx` pinned to the version already in `uv.lock`. Keep `checkout-web` (now `==0.7.0`) and `orjson==3.9.10`. Catalog and Analytics need httpx only if they call a peer (Analytics, Inventory, Checkout do; Catalog does not).
- [ ] T003 [P] Bump `packages/checkout_web/pyproject.toml` to `version = "0.7.0"` and add `httpx` (same pin as T002).
- [ ] T004 Update the root `pyproject.toml` workspace: remove `checkout-data` from `project.dependencies` and `[tool.uv.sources]` (the directory stays until T048 but is no longer a workspace dependency of anything except itself). Run `uv lock && uv sync`, then confirm `uv run python -c "import checkout_web, catalog_service, checkout_service, inventory_service, analytics_service"` succeeds.

---

## Phase 2: Foundational (blocking prerequisites for every story)

**Purpose**: The data-free shared pieces and the test harness that all four services and all tests use.

- [ ] T005 [P] Move `cents_to_amount` from `packages/checkout_data/src/checkout_data/money.py` into `packages/checkout_web/src/checkout_web/money.py`. Leave `catalog_price_cents` out: it is Catalog's seed formula and moves to `services/catalog/src/catalog_service/seed.py` in T014. Add `tests/unit/test_money.py` imports from the new location (the assertions stay).
- [ ] T006 [P] Add `packages/checkout_web/src/checkout_web/client.py` implementing [contracts/internal-apis.md](./contracts/internal-apis.md) "Client behavior":
  - class `ServiceClient(base_url, *, transport=None)` wrapping one `httpx.AsyncClient` with keep-alive, connect timeout `0.5` s, default read timeout `2` s.
  - `request(method, path, *, json=None, timeout=None, retries=0, idempotent=False)` forwards `X-Request-ID` from the current context (T007), retries only when `idempotent=True`, with a total time budget argument, and raises `UpstreamUnavailable` when retries are exhausted or the connection fails.
  - `transport` is injectable so tests can pass the in-process mesh transport (T010).
- [ ] T007 [P] Add request-id support in `packages/checkout_web/src/checkout_web/request.py` (extend the existing file): a `contextvars` holder, an ASGI middleware that reads `X-Request-ID` or generates one, sets the holder and echoes the header on the response, and a `log_step(log, *, step, tx=None, outcome, elapsed_ms=None, **extra)` helper that writes one JSON log line with `request_id`, `service`, `step`, `tx`, `outcome`, `elapsed_ms` ([research R12](./research.md)). Register the middleware in `checkout_web/app.py` `create_service_app`.
- [ ] T008 Add `UpstreamUnavailable` to `packages/checkout_web/src/checkout_web/errors.py` with a handler returning `503` and the contract body `{"error": "INTERNAL_ERROR", "message": "<peer> service is unavailable."}`. Add a unit test `tests/unit/test_client.py` covering: success, timeout then retry then success, retries exhausted raises `UpstreamUnavailable`, non-idempotent call is never retried, and `X-Request-ID` is forwarded.
- [ ] T009 [P] Write `config.py` for each service (`services/<svc>/src/<svc>_service/config.py`), a frozen dataclass settings object following the shape of `packages/checkout_data/src/checkout_data/config.py` but containing only that service's settings:
  - **catalog**: `database_url`, `catalog_size`, `db_pool_size`, `workers`.
  - **checkout**: `database_url`, `catalog_url`, `inventory_url`, `analytics_url`, `tx_abandon_minutes`, `recovery_lease_s=10`, `recovery_interval_s=2`, `reserve_budget_s=4`, `low_stock_threshold` is **not** here (Inventory owns it).
  - **inventory**: `database_url`, `catalog_url`, `catalog_size`, `stock_per_item`, `low_stock_threshold=50`.
  - **analytics**: `database_url`, `catalog_url`, `popular_window_size=1000`, `popular_slide_interval=500`, `popular_poll_ms=100`, `popular_gap_grace_ms=1000`.
  - Each reads `<OWN_NAME>_DATABASE_URL` then `DATABASE_URL` (ground rules).
- [ ] T010 Rewrite `tests/_dispatch.py`:
  - Keep `ROUTES` and `route()`, add `("prefix", "/internal/", "blocked")` and make the dispatcher answer `404 {"error":"NOT_FOUND","message":"Not found."}` for it ([contracts/gateway-routes.md](./contracts/gateway-routes.md)).
  - Add `MeshTransport(httpx.AsyncBaseTransport)` that routes by hostname (`catalog`, `inventory`, `analytics`, `checkout`) straight to each service's ASGI app (no blocking of `/internal/`, since the mesh is private).
  - Add fault hooks: `mesh.fail(target, path_prefix, *, times=1, mode="error"|"timeout"|"drop_response", after=0)` and `mesh.calls` (a recorded list of `(target, method, path)`), so failure tests can assert call counts. `drop_response` performs the call, then raises a timeout (lost acknowledgement).
- [ ] T011 Rewrite `tests/conftest.py` for four databases:
  - Create `checkout_catalog_test`, `checkout_checkout_test`, `checkout_inventory_test`, `checkout_analytics_test` (names must end in `_test`; keep the safety check). Set `CATALOG_DATABASE_URL`, `CHECKOUT_DATABASE_URL`, `INVENTORY_DATABASE_URL`, `ANALYTICS_DATABASE_URL` before importing any service.
  - Per-service fixtures `catalog_session`, `checkout_session`, `inventory_session`, `analytics_session` (each from that service's own `db.py`).
  - `reset_db` runs each service's `seed.py --reset` against its own database (T014–T017) and clears each in-memory catalog cache.
  - The `client` fixture builds the gateway twin plus the mesh and gives every service client the `MeshTransport`. Expose a `mesh` fixture for fault injection.
  - Keep `GATEWAY_URL` mode (same tests over real HTTP).
  - Keep the per-test `engine.dispose()` fixture, once per service engine.

**Checkpoint**: shared pieces and harness exist; nothing is wired yet.

---

## Phase 3: User Story 1 — Unchanged checkout experience on microservices (Priority: P1) 🎯 MVP

**Goal**: The existing contract and integration tests pass against four services with four databases, through the gateway twin.

**Independent Test**: `uv run pytest tests/contract tests/integration` passes (assertions unchanged) with `tests/failure` not yet present.

### Catalog

- [ ] T012 [P] [US1] Create `services/catalog/src/catalog_service/db.py` (async engine from `config.database_url`, `SessionLocal`, `get_session`, `ping`, `dispose_engine`, ported from `packages/checkout_data/src/checkout_data/__init__.py` without the autocommit sessions unless Catalog needs them) and `models.py` with `Base` and `CatalogItem` (`sku` text PK, `name` text not null, `price_cents` int not null, `CheckConstraint("price_cents > 0", name="catalog_item_price_cents_check")`). No other table.
- [ ] T013 [P] [US1] Create `services/catalog/src/catalog_service/repo.py` with `load_catalog(session)` returning `(sku, name, price_cents)` in ascending SKU order (port of `checkout_data/repos/catalog.py`). Update `api.py` and `main.py` to use the local modules; the `/items` body and pre-rendering are unchanged ([research R6](./research.md) keeps `GET /items` as the one catalog read API).
- [ ] T014 [US1] Create `services/catalog/src/catalog_service/seed.py` (`--reset`, `--if-empty`, default) that creates the Catalog schema and seeds `CATALOG_SIZE` items with name `Item {i}` and price from `catalog_price_cents(i)` (copy the function from `checkout_data/money.py`). Mirror the CLI behaviour documented in `checkout_data/seed.py` but touch only `catalog_item`.

### Inventory

- [ ] T015 [P] [US1] Create `services/inventory/src/inventory_service/db.py` (as T012) and `models.py` with `Base` and these tables ([data-model.md](./data-model.md) §3):
  - `InventoryStock`: `sku` text PK, `current_stock` int not null, `initial_stock` int not null, `CheckConstraint("current_stock >= 0", name="inventory_stock_non_negative")`. No foreign key to any catalog table.
  - `StockAdjustment`: `transaction_id` bigint PK, `state` enum `APPLIED/RELEASED` not null, `lines` JSONB not null, `created_at` and `updated_at` timestamptz not null default now.
  - `LowStockAlert`: `id` bigint identity PK, `sku` text, `current_stock` int, `threshold` int, `triggered_at` timestamptz default now, index `low_stock_alert_sku_time_idx` on `(sku, triggered_at DESC)`. No foreign key.
- [ ] T016 [US1] Create `services/inventory/src/inventory_service/reserve.py` implementing [research R2](./research.md) and [contracts/internal-apis.md](./contracts/internal-apis.md):
  - `reserve(session, tx_id, lines, threshold)`: (a) select the `stock_adjustment` row for `tx_id`: `APPLIED` returns `replayed=True`, `RELEASED` returns `released`; (b) otherwise run the spec 006 single statement from `checkout_data/repos/checkout.py` `_COMPLETE_BASKET`, with the `fin` CTE (transaction update) removed and an `adj` CTE added that inserts `stock_adjustment(transaction_id, 'APPLIED', lines)` under the same `(SELECT ok FROM ok)` guard; (c) catch a unique violation on `stock_adjustment` and treat it as a replay.
  - `release(session, tx_id)`: one statement that sets an `APPLIED` row to `RELEASED` and adds each line's quantity back to `current_stock` (sorted by SKU for lock order); if no row exists, insert a `RELEASED` tombstone with empty `lines`. Returns `unitsRestored` and `alreadyReleased`.
  - Neither function commits; the caller owns the transaction (same rule as 005 repos).
- [ ] T017 [US1] Create `services/inventory/src/inventory_service/repo.py` with `low_stock_report(session, threshold)` ported from `checkout_data/repos/inventory.py`, but with **no join to a catalog table**: it returns `(sku, current_stock, triggered_at)` and `low_stock.py` fills in names from the catalog snapshot (T018). Keep ordering by alert time then SKU.
- [ ] T018 [P] [US1] Create `services/inventory/src/inventory_service/catalog_client.py`: a lazy in-memory snapshot of Catalog's `GET /items` using `checkout_web.client.ServiceClient` ([research R6](./research.md)): load on first need, one refresh per 5 s on an unknown SKU, `name_of(sku)` falling back to the SKU, raises `UpstreamUnavailable` only when the cache is empty and Catalog is unreachable. Item order from Catalog is ascending SKU.
- [ ] T019 [US1] Wire Inventory: update `low_stock.py` and `api.py` for the new repo and name lookup; add `POST /internal/reservations` and `DELETE /internal/reservations/{transactionId}` routes in a new `services/inventory/src/inventory_service/internal_api.py` per [contracts/internal-apis.md](./contracts/internal-apis.md) (status codes `200`, `409 INSUFFICIENT_STOCK` with `shortSkus`, `409 RESERVATION_RELEASED`); the `transactionId` path value is the public `tx-<n>` form. Update `main.py` (own lifespan, no catalog load at startup, probe = `ping`).
- [ ] T020 [US1] Create `services/inventory/src/inventory_service/seed.py`: create the Inventory schema and insert stock rows for `SKU-{i:06d}` for `i` in `1..CATALOG_SIZE` with `current_stock = initial_stock = STOCK_PER_ITEM` (the deterministic SKU convention from [research R9](./research.md); no data is read from Catalog). Same CLI modes as T014.

### Analytics

- [ ] T021 [P] [US1] Create `services/analytics/src/analytics_service/db.py` (as T012, own pool) and `models.py` with `Base`, `ScanEvent` (`scan_id` bigint PK **not an identity column**, `sku` text not null, `received_at` timestamptz default now) and `PopularWindowSnapshot` copied from `checkout_data/models.py` (`id` smallint PK default 1 with `CheckConstraint("id = 1", name="popular_window_snapshot_single_row")`, and the same other columns).
- [ ] T022 [US1] Create `services/analytics/src/analytics_service/repo.py` by porting `checkout_data/repos/analytics.py`, replacing every `transaction_item` reference with `scan_event` and `id` with `scan_id`. Add `insert_scans(session, rows) -> (accepted, duplicates)` using `INSERT ... ON CONFLICT (scan_id) DO NOTHING`.
- [ ] T023 [P] [US1] Create `services/analytics/src/analytics_service/catalog_client.py` (same snapshot cache as T018).
- [ ] T024 [US1] Port `pipeline.py`, `supervisor.py`, `popular_items.py` and `api.py` to the local `config`, `db`, `repo`, `catalog_client` (names come from the snapshot instead of `checkout_data.catalog`). The pipeline logic does not change. Add `ingest.py` with `POST /internal/scans` per [contracts/internal-apis.md](./contracts/internal-apis.md) (batch ≤ 1000, validates `scanId` and `sku`), include its router in `main.py`, and drop the startup catalog load (probe = `ping`).
- [ ] T025 [US1] Create `services/analytics/src/analytics_service/seed.py`: create the Analytics schema, no data. Modes as T014 (`--reset` also clears `scan_event` and the snapshot).

### Checkout

- [ ] T026 [P] [US1] Create `services/checkout/src/checkout_service/db.py` (as T012 plus the autocommit sessions from `checkout_data/__init__.py`, since scans and starts use them) and `models.py` with `Base` and, per [data-model.md](./data-model.md) §2:
  - `Transaction`: `id` bigint identity PK, `station_id` text not null, `status` enum `OPEN/COMPLETING/COMPLETED/CANCELLED` (server default `OPEN`), `item_count` int default 0, `running_total_cents` bigint default 0, `total_amount_cents` bigint null, `started_at` and `completed_at` (null) timestamptz, partial index `transaction_open_started_idx` on `started_at WHERE status = 'OPEN'`.
  - `TransactionItem`: `id` bigint identity PK (the global scan sequence), `transaction_id` bigint FK → `transaction.id`, `sku` text not null **with no foreign key**, `unit_price_cents` int not null, `scanned_at` timestamptz default now, indexes on `transaction_id` and `sku`.
  - `Completion`: `transaction_id` bigint PK FK → `transaction.id`, `state` enum `RESERVING/RESERVED/COMPLETED/COMPENSATING`, `total_cents` bigint not null, `attempts` int default 0, `updated_at` timestamptz default now, `last_error` text null.
  - `RelayCursor`: `id` smallint PK default 1 with `CheckConstraint("id = 1")`, `last_scan_id` bigint not null default 0, `updated_at` timestamptz default now.
- [ ] T027 [US1] Create `services/checkout/src/checkout_service/repo.py` by porting `checkout_data/repos/checkout.py`: keep `insert_transaction`, `scan_item`, `get_status`, `lock_transaction`, `get_basket`, `get_transaction` (status returned as `OPEN` when `COMPLETING`), `sweep_abandoned`. **Remove** `_COMPLETE_BASKET` and `complete_basket` (they move to Inventory in T016). Add: `mark_completing(session, tx_id, total_cents)` (sets `COMPLETING`, inserts the `completion` row in state `RESERVING`), `set_completion_state`, `finalize(session, tx_id, total_cents)` (`UPDATE transaction SET status='COMPLETED', completed_at=now(), total_amount_cents=:total WHERE id=:tx AND status='COMPLETING' RETURNING completed_at`, plus completion → `COMPLETED`), `reopen(session, tx_id)` (`COMPLETING` → `OPEN`, delete the completion row).
- [ ] T028 [P] [US1] Create `services/checkout/src/checkout_service/catalog_client.py` (same snapshot cache as T018, exposing `get(sku) -> (name, price_cents) | None` like the old `checkout_data.catalog.get`; an unknown SKU triggers at most one refresh per 5 s, then returns `None` so the scan answers `SKU_NOT_FOUND`).
- [ ] T029 [P] [US1] Create `services/checkout/src/checkout_service/inventory_client.py` with `reserve(tx_id, lines)` and `release(tx_id)` on `ServiceClient` (reserve: `idempotent=True`, 2 s read timeout, retries within `reserve_budget_s`; release: same). Return typed results: `Applied(replayed)`, `Insufficient(short_skus)`, `Released`. Anything else is `UpstreamUnavailable`.
- [ ] T030 [P] [US1] Create `services/checkout/src/checkout_service/analytics_client.py` with `send_scans(batch)` (`POST /internal/scans`, idempotent, 2 s read timeout).
- [ ] T031 [US1] Create `services/checkout/src/checkout_service/saga.py` with the **happy path and the insufficient-stock path** of [contracts/completion-saga.md](./contracts/completion-saga.md) steps 1–3:
  - Step 1 in one `session.begin()`: `lock_transaction` (404 / `TRANSACTION_NOT_OPEN` / `EMPTY_BASKET` precedence exactly as in 005 `service.py`, but a `COMPLETING` transaction **joins** instead of erroring), `get_basket`, compute `total_cents` from the stored `unit_price_cents` of each scan (not from the catalog), `mark_completing`.
  - Step 2: `inventory_client.reserve`. On `Insufficient`: `reopen`, raise `INSUFFICIENT_STOCK` with `"Insufficient stock for " + ", ".join(short) + "."` (same text as 005). On any other failure, raise `UpstreamUnavailable` for now (T050 replaces this with compensation).
  - Step 3: `finalize`, then build the receipt (lines carry the stored unit price and the catalog snapshot's name).
  - Log each step with `log_step` (T007).
- [ ] T032 [US1] Update `services/checkout/src/checkout_service/service.py` and `api.py` to the new modules: `scan_item` takes name/price from `catalog_client`, `complete_transaction` delegates to `saga.py`, everything else unchanged. Remove all `checkout_data` imports and `settings.low_stock_threshold`.
- [ ] T033 [US1] Create `services/checkout/src/checkout_service/scan_relay.py` per [research R5](./research.md): a loop that holds an advisory lock in `checkout-db` (single relay per cluster), reads `transaction_item` rows with `id > relay_cursor.last_scan_id` in batches of up to 1000, applies the 005 `TailIngest` contiguous-watermark and gap-grace rules (port the logic from `services/analytics/src/analytics_service/pipeline.py`, same `POPULAR_GAP_GRACE_MS`), sends them with `analytics_client.send_scans`, and advances `relay_cursor` only after a `200`. On any failure it keeps the cursor and retries after a short sleep. Add repo functions for the batch read and cursor update in `repo.py`.
- [ ] T034 [US1] Update `services/checkout/src/checkout_service/main.py` and `sweeper.py`: lifespan starts the sweep loop and the scan relay (recovery arrives in T051); no startup catalog load (probe = `ping`); sweeper only cancels `OPEN` transactions (the SQL already does, `COMPLETING` is untouched).
- [ ] T035 [US1] Create `services/checkout/src/checkout_service/seed.py`: create the Checkout schema (including the `relay_cursor` row with `last_scan_id = 0`), no data. Modes as T014.

### Tests for US1

- [ ] T036 [US1] Update tests that touch the old shared tables so they use the owning service's session fixture. Edit **fixtures and setup code only**; assertions stay:
  - `tests/integration/test_popular_items.py` and `test_persistent_pipeline.py`, `test_pipeline_restart.py`: direct scan inserts go to Analytics' `scan_event` through `analytics_session`.
  - `tests/integration/test_low_stock.py`, `test_completion_atomic.py`, `test_concurrent_completion.py`: stock reads and writes go through `inventory_session`, transaction reads through `checkout_session`.
  - `tests/integration/test_autocommit_sessions.py`: use `checkout_session` and Checkout's `db.py`.
  - `tests/unit/test_tail_ingest_gaps.py`, `test_pipeline_filters.py`, `test_service_locks.py`: update imports to the per-service modules.
  - Add a `drain_relay` fixture in `tests/conftest.py` that runs one relay pass to completion, and call it wherever a test scans and then reads popular-items.
- [ ] T037 [US1] Add `tests/integration/test_reserve_idempotency.py` against Inventory's database: reserve once, reserve again (`replayed=True`, stock reduced once); insufficient stock changes nothing; release restores units once; release before reserve records a tombstone and a later reserve answers `RESERVATION_RELEASED`; concurrent reserves of the same transaction decrement once; many concurrent reserves of the hot SKU never drive stock below zero.
- [ ] T038 [US1] Add `tests/integration/test_scan_relay.py`: relay delivers scans in order, resumes from the cursor after a restart, redelivery of a batch is counted once, an id that never commits is skipped after the grace period, and popular-items reflects the scans after `drain_relay`.
- [ ] T039 [US1] Run `uv run pytest tests/contract tests/integration tests/unit` and fix failures in the new code until it all passes with the contract assertions unchanged (SC-001). Record the pass count in the commit message.

**Checkpoint**: US1 complete. The unchanged contract suite passes on four services and four databases, in-process. This is the MVP.

---

## Phase 4: User Story 2 — Each microservice owns its data and is its own deployable unit (Priority: P1)

**Goal**: Four database containers on private networks, four independent images, and an automated boundary check that proves it.

**Independent Test**: `uv run python scripts/check_boundaries.py` passes; `docker compose up -d --build --wait` brings up the topology; the isolation script shows no service can reach another's database.

- [ ] T040 [P] [US2] Rewrite the four `services/<svc>/Dockerfile`s: copy only `services/<svc>/requirements.lock`, `packages/checkout_web` and `services/<svc>`; no `packages/checkout_data`. Keep `UVICORN_HTTP=httptools` and the existing `CMD`. Each image also runs its own seed: `python -m <svc>_service.seed --if-empty` is the command compose overrides for the `seed-<svc>` job.
- [ ] T041 [P] [US2] Create `tools/Dockerfile` and `tools/verify_invariant.py` placeholder wiring for the `checkout-tools` image (httpx only, installs `checkout_web`), replacing `packages/checkout_data/Dockerfile`. The tool itself is written in T053.
- [ ] T042 [US2] Update `scripts/lock_services.sh` to export the four service locks only (drop the `checkout-data` export) and run it. Update `tests/unit/test_service_locks.py` so it asserts each lock is current and contains no other service's package (boundary rule 10).
- [ ] T043 [US2] Rewrite `compose.yaml` per [contracts/compose-topology.md](./contracts/compose-topology.md): four `postgres:17` services (`catalog-db`, `checkout-db`, `inventory-db`, `analytics-db`) with their own role, password, tuning and `max_connections`; four `seed-<svc>` jobs; four services with only their own `DATABASE_URL` plus the peer URLs from the "Environment per service" table; the `edge`, `mesh` and four `*_data` networks exactly as specified; `tools` on `mesh` only; gateway unchanged apart from T059. No `depends_on` between services; each service depends only on its own seed job.
- [ ] T044 [US2] Update `.env.example` with the per-database credentials (`CATALOG_DB_USER`, `CATALOG_DB_PASSWORD`, and the same for the other three) and rewrite `compose.test.yaml` as a test-stack override (small catalog, `*_test` databases, separate project name and image tags, as in 005).
- [ ] T045 [US2] Rewrite `scripts/check_boundaries.py` for the ten rules in [contracts/service-boundaries.md](./contracts/service-boundaries.md) (use `ast` and `re` only, plus `yaml` if already available, otherwise a small regex over `compose.yaml`; no new dependency). Rewrite `tests/unit/test_boundaries.py` with one passing case and one failing fixture per rule, in temporary directories.
- [ ] T046 [P] [US2] Create `scripts/check_isolation.sh`: for each (service, foreign database host) pair, `docker compose exec <svc> python -c "socket.create_connection((host, 5432), 2)"` must fail; and each service must still connect to its own database. Prints one line per pair and exits non-zero on any reachable foreign database (FR-008, quickstart V4).
- [ ] T047 [US2] Rewrite `scripts/check_independence.sh` for this architecture: rebuild and restart **one** service's image while the others keep running, and confirm the other three report the same container start time and stay healthy (FR-004, FR-006, spec US2 scenario 4).
- [ ] T048 [US2] Delete `packages/checkout_data/` (including its Dockerfile, `requirements.lock`, `ownership.py`, `repos/`, `seed.py`, `verify_invariant.py`), remove `scripts/seed.py` and `scripts/verify_invariant.py` shims that import it (new shims arrive in T053), and remove any remaining references in `README.md`'s file tree. `grep -rn checkout_data .` outside `specs/` must return nothing (boundary rule 3).
- [ ] T049 [US2] Bring up the stack and validate: `docker compose down -v && docker compose up -d --build --wait`; confirm four seeds exited 0 and all services plus the gateway are healthy; run `./scripts/check_isolation.sh`, `./scripts/check_independence.sh`, and `GATEWAY_URL=http://localhost:8080 uv run pytest tests/contract` (quickstart V4, V5). Fix what fails.

**Checkpoint**: US1 and US2 both hold in the real stack. Four databases, private networks, contract tests green over HTTP.

---

## Phase 5: User Story 3 — Multi-store completion is coordinated and all-or-nothing (Priority: P1)

**Goal**: Every failure of a completion step ends in consistent stores; crashes are recovered; the invariant is checked across all stores.

**Independent Test**: `uv run pytest tests/failure` passes, and `verify_invariant` reports PASS after a failure-injection load.

- [ ] T050 [US3] Extend `services/checkout/src/checkout_service/saga.py` to the full [contracts/completion-saga.md](./contracts/completion-saga.md) outcome table:
  - **B** timeout/error/5xx: retry `reserve` within `reserve_budget_s` (4 s); still unknown → `COMPENSATING`, `release`, `reopen`, raise `UpstreamUnavailable` (503).
  - **C** `RESERVATION_RELEASED` → as B without a second release.
  - **D** finalize fails after reserve ok → `COMPENSATING`, `release`, `reopen`, 503.
  - **E** `release` undeliverable → leave the row `COMPENSATING` for recovery, still return 503.
  - `completion.attempts`, `updated_at`, `last_error` updated at every state change; `log_step` at `begin`, `reserve`, `finalize`, `release` with outcomes `ok/rejected/retry/timeout/compensated`.
  - Concurrent duplicate `complete`: the second caller finds `COMPLETING` and re-drives from the recorded state using only idempotent calls.
- [ ] T051 [US3] Create `services/checkout/src/checkout_service/recovery.py` per [research R4](./research.md): every `recovery_interval_s` select `completion` rows with `state != 'COMPLETED'` and `updated_at < now() - lease` using `FOR UPDATE SKIP LOCKED`; `RESERVING` → `release` then `reopen`; `RESERVED` → finalize; `COMPENSATING` → `release` then `reopen`. Start it from `main.py` next to the relay (T034). Expose `run_once()` so tests can drive it deterministically.
- [ ] T052 [P] [US3] Add read-only report endpoints in a new `internal_reports.py` in each of Checkout, Inventory and Analytics, per [contracts/internal-apis.md](./contracts/internal-apis.md) "Read-only reports": Checkout `GET /internal/reports/sold-by-sku` and `/scan-count`, Inventory `GET /internal/reports/stock`, Analytics `GET /internal/reports/scan-count`. Register each router in its `main.py`.
- [ ] T053 [US3] Write `tools/verify_invariant.py` (the checkout-tools entry point, module `verify_invariant`) and a host shim `scripts/verify_invariant.py`: call the report endpoints over HTTP (`CHECKOUT_URL`, `INVENTORY_URL`, `ANALYTICS_URL`, defaulting to the debug ports on localhost) and check the five invariants in [data-model.md](./data-model.md) §6: per-SKU `initial − current == sold`, no negative stock, no `COMPLETING` transaction or non-terminal completion, adjustments agree with transaction states, and (after waiting up to 30 s for the relay to drain) equal scan counts. Print the same summary style as the 005 tool, exit 0 on PASS and 1 on FAIL.
- [ ] T054 [US3] Create `tests/failure/__init__.py` and `tests/failure/test_saga_outcomes.py`, one test per row A–H of the outcome table (H and F/G via `recovery.run_once()` after simulating the crash by stopping the saga after a given step). Each test injects the fault with `mesh.fail(...)`, then asserts the shopper-visible status code, the final transaction status, `stock_adjustment` state, remaining stock, and that no `completion` row is left non-terminal. A shared helper `assert_consistent()` runs the same checks as T053's invariants against the test databases.
- [ ] T055 [P] [US3] Add `tests/failure/test_saga_idempotency.py`: ten sequential `complete` retries leave one stock decrement and one `COMPLETED` (SC-004), a `reserve` whose response is dropped (`mode="drop_response"`) is retried without a second decrement, two simultaneous `complete` calls produce one decrement, a late `reserve` after a `release` is refused, and a `complete` after completion returns `409 TRANSACTION_NOT_OPEN`.
- [ ] T056 [P] [US3] Add `tests/failure/test_relay_and_ingest_faults.py`: Analytics ingest failing for N batches does not affect scans or completions (Checkout still answers 200), the relay catches up afterwards with every scan counted once, and a dropped ingest response (applied but not acknowledged) is redelivered and counted once.
- [ ] T057 [US3] Add a hot-SKU concurrency test to `tests/failure/test_saga_outcomes.py` (or its own file): 50 concurrent baskets all containing `SKU-000001` with limited stock; afterwards stock is never negative, exactly the available units are sold, and `assert_consistent()` passes.
- [ ] T058 [US3] Run `uv run pytest tests/failure tests/integration` and fix until green. Then run the compose drill (quickstart V8): start a short load, `docker compose kill checkout && docker compose up -d checkout`, wait 15 s, run `scripts/verify_invariant.py`; it must report PASS.

**Checkpoint**: US1–US3 hold. Completion is all-or-nothing across stores, and recovers from crashes.

---

## Phase 6: User Story 4 — Failures stay contained and observable (Priority: P2)

**Goal**: Gateway hides internal routes and stamps request ids; outages are isolated and fast; one id traces a request across services.

**Independent Test**: Stop each service in turn (quickstart V7) and trace one completion by request id (V6).

- [ ] T059 [P] [US4] Update `gateway/nginx.conf` per [contracts/gateway-routes.md](./contracts/gateway-routes.md): add `location /internal/ { default_type application/json; return 404 '{"error":"NOT_FOUND","message":"Not found."}'; }` and `proxy_set_header X-Request-ID $request_id;` in the shared block (keep the rule that no location sets its own `proxy_set_header`).
- [ ] T060 [US4] Update `tests/unit/test_gateway_routes.py` so the nginx route table including `/internal/` is parsed and compared with `tests/_dispatch.py` `ROUTES`, and add a test that the shared block contains `X-Request-ID`.
- [ ] T061 [P] [US4] Add `tests/integration/test_request_id.py`: a request carrying `X-Request-ID: t-1` that completes a transaction produces log records with `request_id == "t-1"` from Checkout (saga steps) and Inventory (the reservation), captured with `caplog`, and a request without the header gets a generated id echoed on the response.
- [ ] T062 [P] [US4] Add `tests/integration/test_timeouts.py`: a `mesh.fail(..., mode="timeout")` on Inventory makes `complete` return `503` within the saga budget plus one release attempt (assert elapsed < 7 s with test-sized timeouts configured through the settings), and a slow Analytics ingest never delays a scan response.
- [ ] T063 [US4] Extend `scripts/check_independence.sh` (T047) with the outage drills of [quickstart.md](./quickstart.md) V7: for each of catalog, inventory, analytics and checkout, `docker compose stop`, assert which routes still succeed and which return the contract `503` body within 5 s (the table is in the script header), restart, wait healthy; for Analytics also scan 500 items during the outage and check popular-items catches up within 30 s of restart (SC-006).
- [ ] T064 [P] [US4] Add `scripts/trace_request.sh <request-id>` that greps `docker compose logs` of all four services for the id and prints the lines ordered by timestamp (quickstart V6), and use it in the V6 walk-through.
- [ ] T065 [US4] Run `./scripts/check_independence.sh` and the V6 trace against the live stack; fix what fails.

**Checkpoint**: US1–US4 hold.

---

## Phase 7: User Story 5 — Graded load reports (Priority: P2)

**Goal**: The two load-client JSON reports, the comparison with spec 005, and the README the spec requires.

**Independent Test**: Both reports exist under `../load-client/reports/007-microservices-architecture/`, the invariant passed after each, and the README names every service and data store.

- [ ] T066 [US5] Update `scripts/run_load_tests.sh`: default `REPORT_DIR` is `007-microservices-architecture`, and the post-run check calls `scripts/verify_invariant.py` (T053) instead of the old tool. Everything else (fresh `down -v`, exact assignment flags) is unchanged.
- [ ] T067 [US5] Run `./scripts/run_load_tests.sh all` on a quiet machine with Docker Desktop at ≥ 8 CPUs and ≥ 8 GB. Confirm zero transport failures, only contract business errors, and invariant PASS after each run (SC-002). If the stress run fails the invariant or shows server errors, fix the cause (do not weaken the check) and rerun.
- [ ] T068 [US5] Add a "Week 7 — microservices" section to `../load-client/reports/README.md`: table of the two reports (tx/s, items/s, COMPLETE error %, invariant), the machine and Docker settings, the 005 stress figure (191 tx/s from the 006 investigation) next to this run's, and the ratio. If the ratio is below 70 % (SC-009), add the measured cause (extra hop, extra commit, four databases) rather than a guess: capture it with a short run that times the saga steps from the `log_step` output.
- [ ] T069 [US5] Rewrite `README.md` for the microservices architecture (SC-010): services table with data stores, infrastructure and data-ownership diagrams, the completion-saga sequence diagram and compensation table (adapt [contracts/completion-saga.md](./contracts/completion-saga.md)), the scan-relay description, "what changed from 005", prerequisites, run and test commands (quickstart V1–V10), and the comparison note from T068. Remove the 005 shared-database sections.

**Checkpoint**: All five stories delivered.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T070 [P] Run the whole of [quickstart.md](./quickstart.md) V1–V10 in order from a clean checkout of the branch and fix any step that does not behave as written (update the quickstart if the doc, not the code, is wrong).
- [ ] T071 [P] Update `../architecture.md`, `../README.md` and `../PIPELINE_ARCHITECTURE.md` only where they still describe the shared database or `checkout_data` as current.
- [ ] T072 Final validation: `uv run python scripts/check_boundaries.py`, `uv run pytest` (full suite), `./scripts/check_independence.sh`, `./scripts/check_isolation.sh`, and `scripts/verify_invariant.py` after the last load run all pass. Commit the reports and push.

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 (Setup)** → **Phase 2 (Foundational)** → all stories.
- **US1** needs Phase 2. It is the MVP and the base for everything else.
- **US2** needs US1 (it containerizes and isolates what US1 built).
- **US3** needs US1 (it hardens the saga US1 started). It does not need US2 for its tests, but T058's compose drill does.
- **US4** needs US2 (compose, gateway) and US3 (the saga logs it traces).
- **US5** needs US2, US3, US4 (a measured run must use the finished system).
- **Polish** needs everything.

### Within a story

- Data layer (`db.py`, `models.py`) before `repo.py`, before the service logic, before routes, before `main.py`.
- Seeds (T014, T020, T025, T035) before `reset_db` can work, so before T036 and T039.
- T016 (reserve) before T019 and T037. T029 (inventory client) before T031 (saga). T033 (relay) needs T026, T027 and T030.
- T050 before T051 (recovery reuses the saga's step functions) and before T054.

### Parallel opportunities

- **Phase 2**: T005, T006, T007, T009 in parallel; T008 after T006/T007; T010 and T011 after the service modules exist (they are mostly independent files, but T011 needs T014–T035 seeds to run).
- **US1**: the four services' data layers are independent. T012, T015, T021, T026 can run together; T013, T018, T023, T028, T029, T030 can run together once their `db.py` exists.
- **US2**: T040, T041, T046 in parallel.
- **US3**: T052, T055, T056 in parallel after T050.
- **US4**: T059, T061, T062, T064 in parallel.

### Parallel example: User Story 1 data layers

```text
Task: "T012 [US1] services/catalog/.../db.py + models.py"
Task: "T015 [US1] services/inventory/.../db.py + models.py"
Task: "T021 [US1] services/analytics/.../db.py + models.py"
Task: "T026 [US1] services/checkout/.../db.py + models.py"
```

## Implementation Strategy

### MVP first (User Story 1)

1. Phases 1–2.
2. US1: split the data layers, wire the happy-path saga and the relay, update fixtures, get the unchanged contract suite green (T039).
3. **Stop and validate.** At this point the architecture is already database-per-service in tests. Failure handling and containers come next.

### Incremental delivery

1. US1 → in-process correctness on four databases.
2. US2 → real containers, private networks, boundary check.
3. US3 → failure handling, recovery, invariant. This is the part the assignment grades hardest, so do not skip it for the reports.
4. US4 → observability and outage drills.
5. US5 → measured reports and README.

## Notes

- **Do not edit contract test assertions.** If one fails, the service is wrong, not the test.
- **Commit after each task or small group**, in the worktree on `007-microservices-architecture`.
- **Spec 006's single-statement stock critical section is the performance-sensitive piece.** T016 must keep its structure (lock in SKU order, check, decrement, alert, all in one statement) with only the transaction update removed and the adjustment insert added.
- **If T067's throughput is under 70 % of 005**, measure before changing the design ([research R14](./research.md)).
