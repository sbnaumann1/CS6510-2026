---
description: "Task list for 005 service-based architecture"
---

# Tasks: Service-Based Architecture (Domain Services over a Shared Database)

**Input**: Design documents from `specs/005-service-architecture/`
**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: These are **included**. The spec requires them: the contract suite through the gateway (SC-001), the boundary check (FR-016), the pipeline window oracle (FR-012b), and the independence script (SC-003).

**Paths**: All paths are relative to `backend/` unless they start with `../` (the repo root, e.g. `../load-client/`). Python is 3.11. Run commands from `backend/`.

**Ground rules for every task**:
- **Moving code**: when a task says "move", copy the file's code verbatim and change only the imports (the old → new module map is in [plan.md](./plan.md) Project Structure).
- **SQL**: SQL lives only in `packages/checkout_data/`.
- **Shared libraries**: `checkout_web` never imports `checkout_data.repos` or SQLAlchemy, and `checkout_data` never imports FastAPI or `checkout_web` ([contracts/service-boundaries.md](./contracts/service-boundaries.md)).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 from spec.md

---

## Phase 1: Setup (workspace skeleton)

**Purpose**: Turn `backend/` into a uv workspace with six member packages, all installable but still empty.

- [X] T001 Create the directory skeleton:
  - `packages/checkout_data/src/checkout_data/repos/`
  - `packages/checkout_web/src/checkout_web/`
  - `services/catalog/src/catalog_service/`
  - `services/checkout/src/checkout_service/`
  - `services/inventory/src/inventory_service/`
  - `services/analytics/src/analytics_service/`
  - `gateway/`
  - `logs/`
  
  Give each Python package dir an empty `__init__.py`, and add `logs/` to `.gitignore`.
- [X] T002 [P] Create `packages/checkout_data/pyproject.toml`:
  - `name = "checkout-data"`, `version = "0.5.0"`, `requires-python = ">=3.11"`.
  - dependencies `sqlalchemy[asyncio]==2.0.23`, `greenlet>=3.0.0`, `asyncpg==0.29.0`, `psycopg2-binary==2.9.9`, `python-dotenv==1.0.0`.
  - build backend `hatchling`, with `[tool.hatch.build.targets.wheel] packages = ["src/checkout_data"]`.
- [X] T003 [P] Create `packages/checkout_web/pyproject.toml`: `name = "checkout-web"`, `version = "0.5.0"`, dependencies `fastapi==0.104.1`, `uvicorn[standard]==0.24.0`, `orjson==3.9.10`, `pydantic==2.5.0`. Use the same hatchling layout as T002.
- [X] T004 [P] Create the four service manifests: `services/catalog/pyproject.toml`, `services/checkout/pyproject.toml`, `services/inventory/pyproject.toml` and `services/analytics/pyproject.toml`.
  - Names are `catalog-service`, `checkout-service`, `inventory-service` and `analytics-service`, each `version = "0.5.0"`.
  - Each depends on `checkout-data==0.5.0`, `checkout-web==0.5.0` and `orjson==3.9.10`, with `[tool.uv.sources] checkout-data = { workspace = true }` and `checkout-web = { workspace = true }`.
  - Same hatchling layout, pointing at `src/<name>_service`.
- [X] T005 Rewrite `pyproject.toml` as the workspace root:
  - **Workspace**: `[tool.uv.workspace] members = ["packages/*", "services/*"]`, and `[tool.uv] package = false`.
  - **Dependencies**: `project.dependencies` = the six member names, each with `{ workspace = true }` in `[tool.uv.sources]`.
  - **Unchanged**: keep `[dependency-groups] dev` and `[tool.pytest.ini_options]` exactly as they are.
  
  Run `uv lock && uv sync`, then confirm `uv run python -c "import checkout_data, checkout_web, catalog_service, checkout_service, inventory_service, analytics_service"` succeeds.

---

## Phase 2: Foundational (shared libraries)

**Purpose**: Build `checkout_data` (the only DB coupling point) and `checkout_web` (HTTP conventions). Every user story depends on these.

**⚠️ CRITICAL**: No service can be built until this phase is complete. The old `app/` package stays untouched and working until T031.

- [X] T006 [P] Move `app/config.py` to `packages/checkout_data/src/checkout_data/config.py`. Add these fields to `Settings`:
  - `popular_poll_ms: int = _int("POPULAR_POLL_MS", 100)`
  - `popular_gap_grace_ms: int = _int("POPULAR_GAP_GRACE_MS", 1000)`
  - `service_name: str = os.getenv("SERVICE_NAME", "dev")`
  
  Keep every existing default, including the Unix-socket `DATABASE_URL`. Compose overrides it.
- [X] T007 [P] Move `app/models.py` to `packages/checkout_data/src/checkout_data/models.py` and `app/money.py` to `packages/checkout_data/src/checkout_data/money.py`, verbatim. Don't move `app/schemas.py`: nothing imports it.
- [X] T008 Move `app/db/__init__.py` to `packages/checkout_data/src/checkout_data/__init__.py`, keeping `engine`, `SessionLocal`, `get_session`, `advisory_lock`, `dispose_engine` and `POPULAR_RECOMPUTE_LOCK = 0x5C40_0001`, with imports from `checkout_data.config`. Add:
  - `async def ping() -> None` (opens a session, runs `SELECT 1`), used by every service's health probe.
  - A comment that `POPULAR_RECOMPUTE_LOCK` now marks the persistent-pipeline leader.
  
  Depends on T006.
- [X] T009 [P] Create `packages/checkout_data/src/checkout_data/ownership.py`.
  - Define `@dataclass(frozen=True) class TableOwnership: owner: str; readers: frozenset[str]` and `SERVICES = ("catalog", "checkout", "inventory", "analytics")`.
  - Define `TABLES: dict[str, TableOwnership]` exactly as in data-model.md §1:
    - `catalog_item` owner `catalog`, readers `{catalog, checkout, inventory, analytics}`
    - `inventory_stock` owner `checkout`, readers `{checkout, inventory}`
    - `transaction` owner `checkout`, readers `{checkout}`
    - `transaction_item` owner `checkout`, readers `{checkout, analytics}`
    - `low_stock_alert` owner `checkout`, readers `{inventory}`
    - `popular_window_snapshot` owner `analytics`, readers `{analytics}`
  - Add a module docstring saying the seed and verify CLIs are exempt.
- [X] T010 [P] Create `packages/checkout_data/src/checkout_data/repos/__init__.py` (empty) and `repos/catalog.py`. `catalog.py` has `SERVICE = "catalog"` and `async def load_catalog(session) -> Sequence[Row]`, running `select(CatalogItem.sku, CatalogItem.name, CatalogItem.price_cents).order_by(CatalogItem.sku)` (the query from `app/catalog_cache.load`).
- [X] T011 Create `packages/checkout_data/src/checkout_data/catalog.py`, the catalog cache moved from `app/catalog_cache.py` **minus** `_items_body`/`items_body()`.
  - It keeps `_by_sku`, `async load(session) -> int` (calls `repos.catalog.load_catalog`), `get(sku)` and `name_of(sku)`.
  - It adds `rows() -> list[tuple[str, str, int]]`, returning (sku, name, price_cents) in ascending SKU order, for the catalog service to render.
  
  Depends on T010.
- [X] T012 [P] Move `app/db/transactions_repo.py` verbatim to `packages/checkout_data/src/checkout_data/repos/checkout.py`, adding `SERVICE = "checkout"` after the imports.
- [X] T013 [P] Create `packages/checkout_data/src/checkout_data/repos/inventory.py` with `SERVICE = "inventory"`. Move `_LOW_STOCK` (and its comment), `_NOW`, `low_stock_report(session, threshold)` and `now(session)` verbatim from `app/db/analytics_repo.py`.
- [X] T014 [P] Create `packages/checkout_data/src/checkout_data/repos/analytics.py` with `SERVICE = "analytics"`.
  - Move `_MAX_SEQ`, `_GET_SCANS`, `_WINDOW_COUNTS`, `_UPSERT`, `_READ` and `_NOW`, plus `max_scan_seq`, `get_scans_after`, `window_counts`, `upsert_snapshot`, `read_snapshot` and `now`, from `app/db/analytics_repo.py`.
  - Change `get_scans_after`'s default `limit` from 100 to **1000**.
  - Don't move `_LOW_STOCK` or `low_stock_report`.
- [X] T015 Move `scripts/seed.py` to `packages/checkout_data/src/checkout_data/seed.py` with `def main(argv: list[str] | None = None) -> int` and `if __name__ == "__main__": sys.exit(main())`. Add a mutually exclusive flag `--if-empty`:
  - Before any DDL, run `SELECT to_regclass('public.catalog_item')`.
  - If the table exists **and** `SELECT count(*) FROM catalog_item` > 0, print `catalog already seeded (<n> items); skipping` and return 0.
  - Otherwise run `Base.metadata.create_all` and seed as normal.
  
  `--reset` behavior is unchanged. Depends on T006–T008.
- [X] T016 [P] Move `scripts/verify_invariant.py` to `packages/checkout_data/src/checkout_data/verify_invariant.py` with a `main()` and `__main__` guard, so `python -m checkout_data.verify_invariant` works. Output and exit codes are unchanged.
- [X] T017 Replace `scripts/seed.py` and `scripts/verify_invariant.py` with thin shims: `from checkout_data.seed import main; sys.exit(main())`, and the same for verify. Command-line flags are identical, so `tests/conftest.py::_seed` and humans keep working. Depends on T015, T016.
- [X] T018 [P] Move `app/errors.py` verbatim to `packages/checkout_web/src/checkout_web/errors.py` (all codes, `ApiError`, `register_error_handlers`).
- [X] T019 [P] Move `app/api/_request.py` to `packages/checkout_web/src/checkout_web/request.py`, importing from `checkout_web.errors`.
- [X] T020 [P] Create `packages/checkout_web/src/checkout_web/health.py` with `def health_router(service: str, probe: Callable[[], Awaitable[None]]) -> APIRouter`. It provides `GET /healthz`:
  - It awaits `probe()`.
  - On success it returns `200 {"status":"ok","service":service}`.
  - On any exception it returns `503 {"error":"INTERNAL_ERROR","message":"<service> is not ready."}`.
  - No DB import: each service passes its own probe.
- [X] T021 Create `packages/checkout_web/src/checkout_web/app.py` with `def create_service_app(name: str, routers: list[APIRouter], probe, lifespan=None) -> FastAPI`.
  - It sets `title=f"Supermarket Self-Checkout API — {name}"`, `version="1.0.0"` and `default_response_class=ORJSONResponse`.
  - It calls `register_error_handlers(app)`, includes each router, then `health_router(name, probe)`.
  
  Depends on T018, T020.

**Checkpoint**: `uv run python -c "import checkout_data.repos.checkout, checkout_data.seed, checkout_web.app"` works, and `uv run pytest -q` still passes against the old `app/`.

---

## Phase 3: User Story 1: Unchanged checkout through independently deployed services (P1) 🎯 MVP

**Goal**: All seven contract operations are served by four separate service apps behind the nginx gateway in the compose stack, with identical behavior. Popular-items is answered by the existing on-demand path (the persistent pipeline arrives in US3).

**Independent Test**: Run quickstart V1 and V3: `docker compose up -d --build --wait`, the in-process suite green, the gateway-mode contract suite green, plus a default load-client run with the invariant passing.

### Services

- [X] T022 [P] [US1] Create the catalog service:
  - **`services/catalog/src/catalog_service/api.py`**: `router` with `GET /items` returning `Response(content=_body, media_type="application/json")`. Define `def render(rows) -> bytes` that builds `orjson.dumps({"items":[{"sku":s,"name":n,"price":cents_to_amount(c)} …]})` in the given (ascending SKU) order, the logic from `app/catalog_cache.load`.
  - **`main.py`**: the lifespan loads `checkout_data.catalog`, renders the body once, logs a warning `catalog is empty — run the seed` if 0 items, and disposes the engine on exit. The probe is `ping()` plus `catalog.size() > 0`; add `size()` to `checkout_data/catalog.py` if T011 omitted it. Finish with `app = create_service_app("catalog", [api.router], probe, lifespan)`.
- [X] T023 [US1] Move `app/transactions/service.py` to `services/checkout/src/checkout_service/service.py`:
  - **Imports**: `checkout_data.catalog` (replaces `app.catalog_cache`), `checkout_data.repos.checkout as repo`, `checkout_data.config.settings`, `checkout_data.money`, `checkout_web.errors`, and `.alerts`.
  - **`scan_item`**: change it to return only the ScanResult dict, dropping `scan_seq`. Nothing consumes the sequence any more.
- [X] T024 [P] [US1] Move `app/transactions/alerts.py` to `services/checkout/src/checkout_service/alerts.py` (using `checkout_data.repos.checkout.insert_alerts`), and `app/transactions/sweeper.py` to `services/checkout/src/checkout_service/sweeper.py` (using `checkout_data.SessionLocal` and `repos.checkout`).
- [X] T025 [US1] Move `app/api/transactions.py` to `services/checkout/src/checkout_service/api.py`, with imports from `checkout_web.request`, `checkout_data.get_session` and `.service`. **Delete** the `BackgroundTasks` parameter, `from app.analytics import scheduler`, and the `should_recompute`/`add_task` lines: the scan route just returns `ORJSONResponse(content=await svc.scan_item(...))`. Depends on T023.
- [X] T026 [US1] Create `services/checkout/src/checkout_service/main.py`:
  - The lifespan loads `checkout_data.catalog` (with the same empty-catalog warning) and starts `_sweep_loop()`, moved verbatim from `app/main.py` and calling `sweeper.sweep_abandoned`.
  - On exit it cancels the sweeper and calls `dispose_engine()`.
  - The probe is `ping()` plus `catalog.size() > 0`.
  - Finish with `app = create_service_app("checkout", [api.router], probe, lifespan)`.
  
  Depends on T024, T025.
- [X] T027 [P] [US1] Create the inventory service:
  - `services/inventory/src/inventory_service/low_stock.py`: moved from `app/analytics/low_stock.py`, using `checkout_data.repos.inventory`.
  - `api.py`: moved from `app/api/inventory.py`, using `checkout_web.request.positive_int_param`.
  - `main.py`: no catalog cache; the probe is `ping()`; the lifespan only disposes the engine. Finish with `app = create_service_app("inventory", …)`.
- [X] T028 [P] [US1] Create the analytics read path:
  - **`popular_items.py`**: moved from `app/analytics/popular_items.py`, using `checkout_data.catalog.name_of` and `checkout_data.repos.analytics`. Keep `read()` and `_render()` unchanged, and keep `recompute()` for now (T060 removes it).
  - **`api.py`**: moved from `app/api/analytics.py`.
  - **`main.py`**: the lifespan loads `checkout_data.catalog` and disposes the engine on exit; the probe is `ping()` plus `catalog.size() > 0`. Finish with `app = create_service_app("analytics", …)`.
  
  Do **not** move `app/analytics/scheduler.py`. It is deleted in T031.

### Test harness (in-process mode)

- [X] T029 [US1] Create `tests/_dispatch.py`, the test-only gateway twin.
  - **Route table**: `ROUTES = [("exact", "/items", "catalog"), ("prefix", "/transactions", "checkout"), ("prefix", "/inventory/", "inventory"), ("prefix", "/analytics/", "analytics"), ("default", "/", "checkout")]`. This mirrors [contracts/gateway-routes.md](./contracts/gateway-routes.md) (`/gateway/healthz` is nginx-only and excluded).
  - **`route(path) -> str`**: applies nginx semantics: exact first, then the longest matching prefix, then the default.
  - **`build_dispatch_app()`**: returns an ASGI callable. It imports `catalog_service.main.app`, `checkout_service.main.app`, `inventory_service.main.app` and `analytics_service.main.app` into `apps = {"catalog": …, …}`, and forwards each `http` scope to `apps[route(scope["path"])]` (`await apps[name](scope, receive, send)`).
- [X] T030 [US1] Rewrite `tests/conftest.py`. Keep the `_test` database safety net, `_ensure_database`, `_seed`, `_schema`, `reset_db`, `session`, `catalog_size` and `stock_per_item`, with imports moved to `checkout_data` (`from checkout_data import SessionLocal, engine`, `from checkout_data.config import settings`). Then:
  1. **`client` default**: `AsyncClient(app=build_dispatch_app(), base_url="http://test")`, after `await checkout_data.catalog.load(session)` (the lifespans don't run under ASGITransport).
  2. **`client` in gateway mode**: if `GATEWAY_URL` is set, `AsyncClient(base_url=os.environ["GATEWAY_URL"])` with no catalog load. In that mode the DSN comes from `TEST_DATABASE_URL`, unchanged logic.
  3. **`dispose` fixture**: keep `_dispose_engine_per_loop`.
  
  Depends on T029.
- [X] T031 [US1] Update test imports. Change:
  - `from app.config` → `from checkout_data.config`
  - `from app.money` → `from checkout_data.money`
  - `from app.analytics import popular_items` → `from analytics_service import popular_items`
  - `from app import catalog_cache` → `from checkout_data import catalog as catalog_cache`
  - `tests/integration/test_concurrent_completion.py`'s `from app.main import create_app` → use `tests._dispatch.build_dispatch_app()`
  
  Files: `tests/unit/test_config.py`, `tests/unit/test_money.py`, `tests/integration/test_popular_items.py`, `tests/integration/test_concurrent_completion.py`, and any other `grep -rn "from app" tests` hit. Don't change any assertion.

  Then delete `app/`, `run.sh` and `scripts/run_server.sh`, and confirm `grep -rn "from app\|import app" --include=*.py .` (excluding `.venv`) is empty. Run `uv run pytest -q`: it must be fully green. Depends on T022–T030.

### Containers and gateway

- [X] T032 [P] [US1] Create `gateway/nginx.conf` implementing [contracts/gateway-routes.md](./contracts/gateway-routes.md):
  - **Top level**: `worker_processes auto;` and `events { worker_connections 4096; }`.
  - **In `http`**:
    - `access_log off;` and `resolver 127.0.0.11 valid=5s ipv6=off;`
    - one `upstream` per service (`upstream catalog { zone catalog 64k; server catalog:8000 resolve; keepalive 64; }`, and the same for checkout, inventory and analytics)
  - **In `server { listen 8080; }`**:
    - `location = /items` → `proxy_pass http://catalog;`
    - `location /transactions` → checkout
    - `location /inventory/` → inventory
    - `location /analytics/` → analytics
    - `location = /gateway/healthz { default_type application/json; return 200 '{"status":"ok","service":"gateway"}'; }`
    - `location /` → checkout
  - **In every proxied location**:
    - `proxy_http_version 1.1; proxy_set_header Connection ""; proxy_set_header Host $host;`
    - `proxy_connect_timeout 2s; proxy_read_timeout 15s;`
    - `proxy_intercept_errors off;`
    - `error_page 502 503 504 = @<svc>_down;`
  - **Named location per service**: `location @<svc>_down { default_type application/json; return 503 '{"error":"INTERNAL_ERROR","message":"<svc> service is unavailable."}'; }`
- [X] T033 [P] [US1] Create `gateway/Dockerfile`: `FROM nginx:1.27-alpine`, `COPY gateway/nginx.conf /etc/nginx/nginx.conf`, `EXPOSE 8080`. The build context is `backend/`.
- [X] T034 [P] [US1] Create `tests/unit/test_gateway_routes.py`:
  - Parse `gateway/nginx.conf` with a regex over `location (=\s*)?(\S+)\s*\{` blocks and their `proxy_pass http://(\w+)`, skipping `@` named locations and `/gateway/healthz`.
  - Assert the resulting set of (kind, path, upstream) equals `tests._dispatch.ROUTES`.
  - Assert `route("/transactions/tx-1/items") == "checkout"`, `route("/items") == "catalog"` and `route("/nope") == "checkout"`.
  
  Depends on T029, T032.
- [X] T035 [US1] Create `scripts/lock_services.sh` (executable, `set -euo pipefail`, `cd` to backend):
  - For each `svc` in catalog, checkout, inventory and analytics, run `uv export --frozen --no-dev --no-emit-workspace --no-hashes --package ${svc}-service -o services/${svc}/requirements.lock`.
  - Also run `uv export … --package checkout-data -o packages/checkout_data/requirements.lock`.
  
  Run it and commit the five lock files.
- [X] T036 [P] [US1] Create `services/catalog/Dockerfile`, `services/checkout/Dockerfile`, `services/inventory/Dockerfile` and `services/analytics/Dockerfile`, each with build context `backend/`:
  ```dockerfile
  FROM python:3.11-slim
  ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SERVICE_NAME=<svc> WORKERS=1
  WORKDIR /srv
  COPY services/<svc>/requirements.lock /tmp/req.lock
  RUN pip install --no-cache-dir -r /tmp/req.lock
  COPY packages/ /tmp/packages/
  RUN pip install --no-cache-dir --no-deps /tmp/packages/checkout_data /tmp/packages/checkout_web
  COPY services/<svc>/ /tmp/svc/
  RUN pip install --no-cache-dir --no-deps /tmp/svc && rm -rf /tmp/packages /tmp/svc
  EXPOSE 8000
  CMD uvicorn <svc>_service.main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS} --loop uvloop --http h11 --no-access-log --log-level warning
  ```
  Each file copies only its own `services/<svc>/` (research R3), and the shell-form `CMD` is deliberate, so `${WORKERS}` expands. Depends on T035.
- [X] T037 [P] [US1] Create `packages/checkout_data/Dockerfile` (the `checkout-tools` image): the same pattern, installing `packages/checkout_data/requirements.lock` and then `checkout_data` only, with `CMD ["python", "-m", "checkout_data.seed", "--if-empty"]`. Depends on T035.
- [X] T038 [US1] Create `compose.yaml` per [contracts/compose-topology.md](./contracts/compose-topology.md), and delete `docker-compose.yml`. Every value is in that contract and in data-model.md §4.
  - **`postgres`**:
    - `postgres:17`, env `POSTGRES_DB: ${POSTGRES_DB:-checkout}`, `POSTGRES_USER: checkout` and `POSTGRES_PASSWORD: checkout`.
    - `command` flags `shared_buffers=1GB`, `work_mem=32MB`, `max_connections=200`, `synchronous_commit=off`, `wal_compression=on`, `checkpoint_timeout=15min` and `max_wal_size=8GB`.
    - port `127.0.0.1:5433:5432`, volume `pgdata`.
    - healthcheck `pg_isready -U checkout -d ${POSTGRES_DB:-checkout}` every 2s.
  - **`seed`**: built from `packages/checkout_data/Dockerfile`, image `checkout-tools`. Env `DATABASE_URL`, `CATALOG_SIZE: ${CATALOG_SIZE:-2000}` and `STOCK_PER_ITEM: ${STOCK_PER_ITEM:-10000}`. `depends_on: postgres: condition: service_healthy`, `restart: "no"`.
  - **`catalog`, `checkout`, `inventory`, `analytics`**:
    - each built from its Dockerfile.
    - env `DATABASE_URL: postgresql+asyncpg://checkout:checkout@postgres:5432/${POSTGRES_DB:-checkout}`, plus `WORKERS`/`DB_POOL_SIZE` of catalog 2/2, checkout 6/10, inventory 1/4 and analytics 1/4.
    - also `LOW_STOCK_THRESHOLD` (checkout, inventory), `TX_ABANDON_MINUTES` (checkout), and `POPULAR_WINDOW_SIZE`, `POPULAR_SLIDE_INTERVAL`, `POPULAR_POLL_MS: 100` and `POPULAR_GAP_GRACE_MS: 1000` (analytics).
    - ports `127.0.0.1:8081`–`8084:8000` respectively.
    - `depends_on: seed: condition: service_completed_successfully` only.
    - healthcheck `["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]`, interval 2s, retries 30.
  - **`gateway`**: built from `gateway/Dockerfile`, port `8080:8080`. `depends_on` all four domain services with `condition: service_healthy`. Healthcheck `wget -qO- http://127.0.0.1:8080/gateway/healthz`.
  - **`tools`**: image `checkout-tools`, `profiles: ["tools"]`, the same env as `seed`, `depends_on: postgres` healthy.
  - **Rule**: no service's `environment` names another service's host (boundary rule 6).
  
  Depends on T033, T036, T037.
- [X] T039 [US1] Create `compose.test.yaml`, an override that sets `POSTGRES_DB=checkout_test`, `CATALOG_SIZE=50` and `STOCK_PER_ITEM=100` through the same variables, so `docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait` yields a test stack whose DB is reachable at `127.0.0.1:5433/checkout_test`. Depends on T038.
- [X] T040 [US1] Validate quickstart **V1, V2 and V3** with Docker Desktop running. Fix until:
  - `docker compose up -d --build --wait` exits 0, every long-running unit is healthy, and `seed` exited 0;
  - `curl localhost:8080/items` returns the catalog;
  - `uv run pytest -q` is green;
  - the gateway-mode contract run (`GATEWAY_URL=… TEST_DATABASE_URL=… uv run pytest -q tests/contract`) is green.
  
  Depends on T031, T034, T038, T039.
- [X] T041 [US1] Smoke-run the load client: `docker compose down -v && docker compose up -d --build --wait`, then `cd ../load-client && ./build.sh && ./run.sh --duration=20`, then `docker compose run --rm tools python -m checkout_data.verify_invariant`. Expect the invariant to PASS, and no `INTERNAL_ERROR` or timeouts. Don't commit this report. Depends on T040.

**Checkpoint**: the MVP is live. The whole contract works through four separately containerized services behind the gateway.

---

## Phase 4: User Story 2: Each service is its own deployable unit (P1)

**Goal**: Any one service can be stopped, restarted or rebuilt without touching the others. Outages return the contract's error shape.

**Independent Test**: quickstart V6. `scripts/check_independence.sh` passes (except step 5's catch-up assertion until US3), and the touch-and-rebuild check recreates only `analytics`.

- [X] T042 [P] [US2] Create `tests/unit/test_service_locks.py`. For each of the five lock files from T035, run the same `uv export` command to a temp file and assert it matches the committed file byte for byte. Skip with a reason if `uv` isn't on `PATH`.
- [X] T043 [US2] Create `scripts/check_independence.sh` (executable, `set -euo pipefail`; needs the stack from V1 running). For each `svc` in `analytics catalog inventory`:
  1. `docker compose stop $svc`.
  2. Assert the other routes still succeed through `:8080`:
     - `GET /items` returns 200 (unless svc=catalog).
     - start → scan `SKU-000001` → complete on `/transactions` returns 201/200/200.
     - `GET /inventory/low-stock` returns 200 (unless svc=inventory).
  3. Assert the stopped service's route returns `503` with exactly `{"error":"INTERNAL_ERROR","message":"<svc> service is unavailable."}`, compared with `python3 -c` json equality.
  4. Analytics only: while it's stopped, scan 500 items through `:8080` in one transaction, recording `max(id)` beforehand via `docker compose exec -T postgres psql -U checkout -tAc "select coalesce(max(id),0) from transaction_item"`.
  5. `docker compose start $svc`, then wait until it's healthy (`docker compose ps --format json`, max 60 s).
  6. Analytics only: within 2 s of healthy, assert `GET /analytics/popular-items` reports a `windowEnd` that is the latest multiple of 500 ≤ the current max id (catch-up). This step passes only after US3 (T061).
  7. Print `PASS <svc>`. Exit non-zero on the first failure, printing the response.
  
  Depends on T040.
- [X] T044 [US2] Validate the rebuild independence part of quickstart V6:
  - Record `docker compose ps --format '{{.Service}} {{.CreatedAt}}'`, then `touch services/analytics/src/analytics_service/api.py && docker compose up -d --build --wait`.
  - Assert only `analytics` has a new CreatedAt.
  - If other services were recreated, fix their Dockerfile `COPY` scope (research R3) and re-run.
  
  Depends on T036.

**Checkpoint**: units deploy and fail independently.

---

## Phase 5: User Story 3: Services coordinate only through the shared database (P2)

**Goal**: An enforced boundary check, plus Analytics' **persistent streaming pipeline**. It tails `transaction_item` and slides the window internally, replacing the deleted in-process trigger (research R5–R6, data-model §3, spec FR-012/012a/012b).

**Independent Test**: quickstart V4 (0 violations) and V5 (scans through Checkout on :8082 produce windows on Analytics :8084, with `pipeline started` logged once). Also `check_independence.sh` passes fully, including the analytics catch-up.

### Tests first (write them, watch them fail, then implement)

- [X] T045 [P] [US3] Create `tests/unit/test_pipeline_filters.py`. Drive each filter from `analytics_service.pipeline` with real `asyncio.Queue`s and no DB:
  - **WindowFilter**:
    - Feed ScanData ids 1–1 600 and `Watermark(500)`, `Watermark(1000)`, `Watermark(1500)`. Assert three `Slide`s with `(window_start, window_end)` = (0,500), (0,1000), (500,1500).
    - Assert `added`/`evicted` contain exactly the SKUs of ids (b−500, b] and (b−1500, b−1000].
    - Assert no Slide is emitted without a Watermark.
  - **AggregationFilter**: after any random sequence of Slides, the emitted `counts` equal `Counter` over the ids in (b−1000, b], with no zero entries.
  - **RankingFilter**: counts `{"B":2,"A":2,"C":3}` rank C, A, B (ties by SKU ascending), truncated to 200.
  - **OutputFilter** with a fake repo:
    - put windows 500, 1000, 1500 into the latest-wins slot before the consumer runs, and assert only 1500 is written;
    - with the consumer running, each window is upserted then committed (`commit` called once per write);
    - `window published start=… end=…` is logged.
  - **TERMINATE**: putting it into the first queue makes every filter exit.
- [X] T046 [P] [US3] Create `tests/unit/test_tail_ingest_gaps.py`. Use a fake `get_scans_after(last_id, limit)` backed by a mutable dict of committed `id → sku`, and a fake clock or `monkeypatch` on `POPULAR_GAP_GRACE_MS` (set it to 50 ms for speed):
  1. **Late commit**: commit ids 1, 2, 4, 5. Assert only 1 and 2 are emitted, then commit 3 within the grace period. Assert 3, 4 and 5 are emitted in order and nothing is skipped.
  2. **Permanent gap**: commit 1, 2, 4. After the grace period expires, assert 4 is emitted, `gap skipped id=3` is logged at INFO, and `last_id == 4`.
  3. **Watermarks**: with ids 1–1 000 committed, the emitted stream contains `Watermark(500)` after ScanData 500 and `Watermark(1000)` after ScanData 1000, each exactly once.
  4. **Bootstrap**: starting from `last_id = 1000` with ids 1 001–1 600 committed, the first watermark is 1500.
  5. **Backlog**: with 3 000 ids committed, the tail does not sleep between full batches. Assert the fake was called ≥ 3 times with no `asyncio.sleep` in between (monkeypatch `asyncio.sleep` to record calls).
- [X] T047 [P] [US3] Create `tests/integration/test_persistent_pipeline.py` against the test DB (uses `reset_db` and `client`):
  1. Start `analytics_service.supervisor.PipelineSupervisor(on_publish=published.append)` as a task. The `on_publish` hook is defined in T055/T056.
  2. Open 8 transactions and scan 1 600 items through `client` from 8 concurrent tasks (random SKUs from the catalog), so ids commit out of order.
  3. Poll `read_snapshot` until `window_end == 1500` (timeout 10 s).
  4. Assert `published` is non-empty and its `window_end`s are strictly increasing multiples of 500 ending at 1500. The latest-wins slot may legitimately drop 500 or 1000 under fast scanning, so don't require them.
  5. For **every** RankedData in `published`, assert its ranking equals `repos.analytics.window_counts(session, end-1000, end, 200)` rendered as `[{"sku","scanCount"}]` (FR-012b). Then, in a sequential variant (scan 500, wait for publish, repeat 3×), assert all three windows 500, 1000 and 1500 are published and each matches the oracle.
  6. Assert `supervisor.instances == 1` (it was never restarted per slide, FR-012a).
  7. Assert `GET /analytics/popular-items` through `client` returns `windowEnd == 1500` and `windowStart == 500`.
  8. Cancel the supervisor.
- [X] T048 [P] [US3] Create `tests/integration/test_pipeline_restart.py`:
  - Start the supervisor, scan 700 items, and wait for window 500.
  - Cancel the supervisor, scan 900 more (max id 1 600), and start a **new** supervisor.
  - Assert it logs `pipeline started bootstrap_from=500` (that is, b₀=1500 minus 1000), and publishes `window_end == 1500` with a ranking equal to the `window_counts` oracle.
- [X] T049 [P] [US3] Create `tests/unit/test_boundaries.py`:
  - Run `scripts/check_boundaries.py`'s `check(root: Path) -> list[str]` on `backend/` and assert it returns `[]`.
  - For each of the 7 rules, write a minimal violating tree into `tmp_path` and assert `check(tmp_path)` reports that rule. For example: `services/catalog/src/catalog_service/x.py` with `import checkout_service`, or `packages/checkout_data/src/checkout_data/repos/inventory.py` with `UPDATE inventory_stock`.

### Persistent pipeline (research R5–R6, data-model §3)

- [X] T050 [US3] Rewrite `services/analytics/src/analytics_service/pipeline.py`, moved from `app/analytics/pipeline.py` and then reworked. Start with the item types from data-model §3, as frozen dataclasses:
  - `ScanData(scan_id: int, sku: str)`
  - `Watermark(boundary: int)`
  - `Slide(window_start: int, window_end: int, added: list[str], evicted: list[str])`
  - `WindowCounts(window_start: int, window_end: int, counts: dict[str, int])`
  - `RankedData`, unchanged from 004
  - the `TERMINATE` sentinel
  
  Read `WINDOW_SIZE` and `SLIDE_INTERVAL` from `settings.popular_window_size` / `popular_slide_interval`, keep `RANKING_DEPTH = 200`, and use logger `logging.getLogger("analytics")`.
- [X] T051 [US3] In `services/analytics/src/analytics_service/pipeline.py`, implement `class TailIngest(session_factory, start_after: int, repo=checkout_data.repos.analytics)` with `async run(out: Queue)`. Follow research R6 exactly:
  - **State**: `last_id = start_after`, `pending: dict[int, str]`, `hole_since: float | None`, and `next_boundary` = the smallest multiple of SLIDE_INTERVAL > start_after.
  - **Each loop**:
    - Read `get_scans_after(session, last_id, 1000)` in a short-lived session (commit/close per read so each poll sees newly committed rows), and merge the rows into `pending`.
    - Pop contiguous ids `last_id+1…` into `out` as `ScanData`. After each accepted id, while `next_boundary <= last_id`, emit `Watermark(next_boundary)` and add SLIDE_INTERVAL.
    - **At a hole**: set `hole_since` if None. If `now - hole_since > popular_gap_grace_ms/1000`, log INFO `gap skipped id=%d`, set `last_id += 1` (then emit any watermark it crosses), reset `hole_since`, and continue.
    - **Sleep**: `popular_poll_ms/1000` only when the read returned fewer than 1000 rows.
    - **Shutdown**: exit on cancellation or when `out` receives TERMINATE from the supervisor (the supervisor cancels TailIngest and pushes TERMINATE downstream).
  
  Depends on T050.
- [X] T052 [US3] In `services/analytics/src/analytics_service/pipeline.py`, implement `WindowFilter.run(inq, outq)`:
  - Keep `buffer: deque[ScanData]`, appending each ScanData.
  - On `Watermark(b)`:
    - `added` = SKUs with id in (b−SLIDE, b];
    - `evicted` = SKUs with id in (b−SLIDE−WINDOW, b−WINDOW], popped from the left while `buffer[0].scan_id <= b − WINDOW`;
    - emit `Slide(max(0, b−WINDOW), b, added, evicted)`.
  - Forward TERMINATE and return.
  
  Depends on T050.
- [X] T053 [US3] In `services/analytics/src/analytics_service/pipeline.py`, implement `AggregationFilter.run(inq, outq)` with one persistent `Counter`. For each Slide, `+1` per added and `−1` per evicted, deleting keys that reach 0. Emit `WindowCounts(start, end, dict(counter))`. Depends on T050.
- [X] T054 [US3] In `services/analytics/src/analytics_service/pipeline.py`, implement `RankingFilter.run(inq, outq)`: sort `counts.items()` by `(−count, sku)`, take `[:RANKING_DEPTH]`, and emit `RankedData(window_size=WINDOW_SIZE, slide_interval=SLIDE_INTERVAL, window_start, window_end, computed_at=None, ranking=[{"sku":s,"scanCount":c}…])`. `computed_at=None` means the DB `now()` is used in `upsert_snapshot`. Depends on T050.
- [X] T055 [US3] In `services/analytics/src/analytics_service/pipeline.py`, implement `LatestSlot` (one item, with `put` replacing any unconsumed item and `get` awaiting the next) and `OutputFilter(session_factory, on_publish: Callable[[RankedData], None] | None = None).run(slot)`. `on_publish` is a test hook called after each successful commit. For each RankedData:
  - in a fresh session, call `repo.upsert_snapshot(…, orjson.dumps(ranking).decode(), computed_at)`, then `commit()`;
  - log INFO `window published start=%d end=%d`;
  - skip any item whose `window_end <= last_written_end`.
  
  RankingFilter writes into the `LatestSlot` instead of a queue. Depends on T050.
- [X] T056 [US3] Create `services/analytics/src/analytics_service/supervisor.py` with `class PipelineSupervisor(on_publish=None)` and `async run()`. It sets `self.instances = 0` and passes `on_publish` to each OutputFilter it builds.
  1. **Standby**: open a dedicated `engine.connect()` and loop on `pg_try_advisory_lock(POPULAR_RECOMPUTE_LOCK)` every 2 s until acquired. Hold the connection for the leader's whole lifetime.
  2. **Bootstrap**: `max_seq = repos.analytics.max_scan_seq`, `b0 = max_seq - max_seq % SLIDE`, `start_after = max(0, b0 - WINDOW)`. Log INFO `pipeline started bootstrap_from=%d`, and increment `self.instances`, which tests read.
  3. **Build**: create `q1 = Queue(maxsize=10000)`, `q2 = Queue(maxsize=64)`, `q3 = Queue(maxsize=64)` and a `LatestSlot`, then start the five filters as tasks.
  4. **Error**: if any task raises, log `pipeline restarting after error` with the traceback, cancel all five, sleep 1 s and go back to step 2 (keep the lock).
  5. **Shutdown**: on cancellation, cancel the filters, unlock, and close the connection.
  
  Depends on T051–T055.
- [X] T057 [US3] Wire `PipelineSupervisor` into `services/analytics/src/analytics_service/main.py`: the lifespan starts it as a task after the catalog loads, and cancels and awaits it on exit. Call `logging.getLogger("analytics").setLevel(logging.INFO)` with a stream handler, so INFO lines appear despite uvicorn's `--log-level warning`. Depends on T056.
- [X] T058 [US3] Run T045–T048 until green. Fix the pipeline code, not the tests, unless a test contradicts data-model §3.
- [X] T059 [US3] Delete `recompute()` from `services/analytics/src/analytics_service/popular_items.py`. In `tests/integration/test_popular_items.py`, replace each `await analytics.recompute(session)` (in `test_window_slides_past_older_scans`, `test_recompute_is_idempotent` and the test at old line ~126) with `await run_pipeline_until(end)`. `run_pipeline_until` is a helper added to `tests/conftest.py` that runs `PipelineSupervisor` until `read_snapshot().window_end >= end`, then cancels it. Keep each test's assertions and rename `test_recompute_is_idempotent` → `test_republish_is_idempotent`. Depends on T057.

### Boundary check (research R12, contracts/service-boundaries.md)

- [X] T060 [US3] Create `scripts/check_boundaries.py` with `def check(root: Path) -> list[str]` and a `__main__` that prints violations and `0 violations (7 rules, 4 services, 6 tables)` (or exits 1). Use only `ast` and `re`. The seven rules:
  1. `services/<a>/**/*.py` importing a module whose top-level name is `<b>_service` with b ≠ a.
  2. A service module importing `httpx`, `requests`, `aiohttp`, `urllib.request`, `http.client` or `socket`.
  3. A service importing `checkout_data.repos.<x>` with x ∉ {own name, `catalog`}.
  4. A service importing `text`, `select`, `insert`, `update` or `delete` from `sqlalchemy` (or `sqlalchemy.sql`).
  5. For each `packages/checkout_data/src/checkout_data/repos/<svc>.py`, every string constant matched against:
     - writes `(INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)`, where the table's owner must be `<svc>`;
     - reads `(FROM|JOIN)\s+(\w+)`, where `<svc>` must be in the readers.
     
     Ignore names that aren't keys of `TABLES`, such as CTE aliases (`tx`, `ins`) and `LATERAL`.
  6. `compose.yaml`: no domain service's `environment` value contains the hostname of another domain service or `gateway`. Parse it with a regex over the `environment:` blocks; no PyYAML.
  7. `checkout_data/**` importing `fastapi` or `checkout_web`, or containing `raise ApiError`.
  
  Depends on T009, T038.
- [X] T061 [US3] Validate quickstart **V4** (`uv run python scripts/check_boundaries.py` prints 0 violations) and **V5** (direct :8082 scans produce windowEnd 500 then 1500 on :8084, `pipeline started` count 1, `window published` count ≥ 3). Then run `scripts/check_independence.sh` and confirm all three `PASS` lines, including analytics catch-up step 6. Depends on T043, T049, T057, T060.

**Checkpoint**: no service calls another, and Analytics runs one persistent pipeline fed only by the database.

---

## Phase 6: User Story 4: Graded load reports for the services architecture (P2)

**Goal**: The two committed load-client JSON reports, the services list and the README.

**Independent Test**:
- Two reports exist in `../load-client/reports/005-service-architecture/`, with `baseUrl http://localhost:8080`, and stations/duration of 10/60 and 100/120.
- The invariant passed after each run.
- The README lists the services and links both reports.

- [X] T062 [US4] Rewrite `scripts/run_load_tests.sh`, keeping its structure and summary block. Usage: `[default|stress|all]`, default `all`, and accept `normal` as an alias for `default`. Per run:
  1. `docker compose down -v`, then `docker compose up -d --build --wait`.
  2. `(cd ../load-client && ./build.sh >/dev/null)` once at the start.
  3. Run `./run.sh` for default (no flags) and `./run.sh --stations=100 --duration=120` for stress.
  4. Detect the new `../load-client/reports/report-*.json` (same before/after logic as today) and `mkdir -p` + `mv` it to `../load-client/reports/005-service-architecture/`.
  5. `docker compose logs --no-color > logs/compose-<run>.log`.
  6. `docker compose run --rm tools python -m checkout_data.verify_invariant`, setting FAILED=1 on failure.
  7. Print the existing Python summary.
  
  Remove the `lsof`/`SERVER_PID`/`run_server.sh` logic, and update the trailing "manual equivalent" comment to the compose commands. **Leave the stack running after the last run** and print `stack left up for review; tear down with: docker compose down -v`. T067 queries the stress database and logs.
- [X] T063 [US4] Manual step: set Docker Desktop to ≥ 8 CPUs / ≥ 8 GB, confirm with `docker info --format '{{.NCPU}} {{.MemTotal}}'`, and write the values into a `<!-- docker-resources: … -->` placeholder line in `../load-client/reports/README.md`. T065 expands it.
- [X] T064 [US4] Run `./scripts/run_load_tests.sh all`. Accept the runs only if both of these hold:
  - the invariant PASSes after each;
  - neither report has errors other than COMPLETE 409 `INSUFFICIENT_STOCK`. Check `operations[*].errorCount` and `logs/compose-*.log` for `INTERNAL_ERROR` or tracebacks.
  
  If either fails, fix the cause and re-run both. Keep both reports, and delete superseded attempts from the 005 folder. Depends on T061, T062, T063.
- [X] T065 [P] [US4] Add a "Week 5 — Domain services (compose)" section to `../load-client/reports/README.md` with:
  - the host;
  - the Docker Desktop CPU/memory from T063;
  - the per-service WORKERS/pool values;
  - a table of the two reports: file, scenario, config, tx/s, items/s, COMPLETE err %, invariant;
  - a comparison row against the spec 004 baseline (320.9 / 269.1 tx/s);
  - a short paragraph explaining any gap, citing the gateway hop and Docker Desktop port forwarding (research R9).
  
  Depends on T064.
- [X] T066 [US4] Rewrite `README.md` for Week 5:
  - **Title and intro**: services-based architecture.
  - **Services defined**: a table of the four domain services with responsibility, owned endpoints and background work (from the spec Overview), plus gateway, postgres and seed/tools listed as infrastructure.
  - **Shared database access**: how it's packaged and distributed (`checkout-data` 0.5.0 with per-service repo modules and the ownership registry; `checkout-web`; each image installs both), linking contracts/service-boundaries.md and data-model.md §1.
  - **Analytics**: the persistent pipeline (one paragraph plus the filter diagram from research R5).
  - **Prerequisites**: Docker Desktop and resources.
  - **Run, test and teardown**: from the quickstart.
  - **Load reports**: links to both 005 reports and the README section from T065.
  - **Design artifacts**: links for 005 above the existing 001–004 links.
  
  Remove the instructions for `./run.sh`, `scripts/run_server.sh` and native seeding. Depends on T064.

**Checkpoint**: the grading deliverables are in the repo.

---

## Phase 7: Polish, pre-push gate & cross-cutting

**Purpose**: The mandatory pre-push review (requested 2026-10-13), doc consistency, and a final validation. **Nothing is pushed until T067–T068 are done.**

- [ ] T067 Complete Section 1 of `specs/005-service-architecture/checklists/pre-push-review.md` (no skipped scans), using the stress run from T064:
  1. Count `gap skipped id=` lines in `logs/compose-stress.log`. Collect the ids, and against the still-running stress stack (T062 leaves it up) run `docker compose exec -T postgres psql -U checkout -tAc "select count(*) from transaction_item where id = any('{<ids>}')"`. The result must be 0.
  2. Re-run T047's oracle comparison for the final published window against the stress database.
  3. Re-confirm T048 and the check_independence catch-up results.
  4. Review the TailIngest, WindowFilter and LatestSlot code for back-pressure (only windows are ever dropped) and leader handover.
  
  Check each box, and fill in the Outcome row with the evidence (counts, commands).
- [ ] T068 Complete Section 2 of `specs/005-service-architecture/checklists/pre-push-review.md` (the analytics `LISTEN` decision). From the T064 logs and timings, record:
  - the observed scan-to-`window published` latency (compare log timestamps with the snapshot's `computed_at`);
  - the idle query rate (≈ 1000/`POPULAR_POLL_MS` per second).
  
  Then decide adopt or reject, with the coupling analysis written down. Update research.md R5 "Alternatives considered" with the decision. If it's adopted, stop: add a new spec clarification and the tasks first. Fill in the Outcome row.
- [X] T069 [P] Update the repo-root docs that describe the old pipeline so they match spec 005: `../PIPELINE_ARCHITECTURE.md`, `../PIPELINE_DESCRIPTION.txt` and `../architecture.md`. Cover the persistent filters, TailIngest replacing the per-run IngestFilter, the 4 services plus gateway, and the shared `checkout-data`. Keep their structure, and change only what's now false.
- [X] T070 [P] Remove leftovers: `server-normal.log`/`server-stress.log` if no longer referenced, any `app/` mentions in `scripts/`, and `.env.example` entries that only applied natively. Add the `POPULAR_POLL_MS`/`POPULAR_GAP_GRACE_MS` entries to `.env.example`. Confirm with `grep -rn "app\.\|run_server" scripts README.md`.
- [ ] T071 Final validation: run quickstart **V1–V8** top to bottom on a clean stack, plus `uv run pytest -q` and `uv run python scripts/check_boundaries.py`. Confirm that every box in `checklists/pre-push-review.md` is checked and both Outcome rows are filled. Only then commit, and push the branch.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001–T005)** → **Foundational (T006–T021)** → user stories → **Polish (T067–T071)**.
- **US1 (T022–T041)** depends only on Foundational. It is the MVP.
- **US2 (T042–T044)** depends on US1's containers (T036, T038, T040). Its catch-up assertion (T043 step 6) is completed by US3 (T061).
- **US3 (T045–T061)** depends on US1's services and test harness (T028, T030, T031). The boundary check T060 also needs `compose.yaml` (T038).
- **US4 (T062–T066)** depends on US3 (T061): the graded runs must use the final persistent pipeline.
- **Polish**: T067 and T068 need T064's stress logs, and T071 needs everything.

### Within US3

T045–T049 (tests, parallel) → T050 → T051–T055 (filters; same file, so sequential, in any order) → T056 → T057 → T058 → T059. T060 can go in parallel with T050–T059. T061 comes last.

### Parallel opportunities

- **Setup**: T002, T003 and T004.
- **Foundational**:
  - T006, T007, T009, T010, T012, T013, T014, T016, T018, T019 and T020 are all different files.
  - Then T008 → T011, T015, T017, T021.
- **US1**:
  - T022, T024, T027 and T028 (different service dirs);
  - T032, T033 and T034;
  - T036 and T037 after T035.
- **US3**: T045–T049 together, and T060 alongside the pipeline work.
- **US4/Polish**: T065 alongside T066, and T069 and T070 together.

## Parallel Example: User Story 1

```bash
# After Foundational, carve out the services together:
Task: "T022 [US1] catalog service in services/catalog/src/catalog_service/{api,main}.py"
Task: "T024 [US1] alerts.py + sweeper.py in services/checkout/src/checkout_service/"
Task: "T027 [US1] inventory service in services/inventory/src/inventory_service/"
Task: "T028 [US1] analytics read path in services/analytics/src/analytics_service/"

# Gateway pieces together:
Task: "T032 [US1] gateway/nginx.conf"
Task: "T033 [US1] gateway/Dockerfile"
```

## Parallel Example: User Story 3

```bash
Task: "T045 [US3] tests/unit/test_pipeline_filters.py"
Task: "T046 [US3] tests/unit/test_tail_ingest_gaps.py"
Task: "T047 [US3] tests/integration/test_persistent_pipeline.py"
Task: "T048 [US3] tests/integration/test_pipeline_restart.py"
Task: "T049 [US3] tests/unit/test_boundaries.py"
```

## Implementation Strategy

### MVP first (US1)

1. Setup, then Foundational: the shared libraries exist, and the old `app/` still passes the suite.
2. US1: four service apps, the in-process suite green, `app/` deleted, then the containers and gateway, with V1/V3 green.
3. **Stop and validate**: a smoke load run with the invariant PASS. The system is already a services architecture, with popular-items answered on demand.

### Incremental delivery

1. US2 proves independent deployment and failure.
2. US3 adds the enforced boundaries and the persistent pipeline. That completes the architecture.
3. US4 produces the graded reports, which only make sense on the final architecture.
4. Polish passes the pre-push gate, and only then is the work pushed.

### Notes

- Commit after each task, or after each checkpoint at the latest, on the feature branch. Don't push before T071 (memory: pre-push review).
- [P] means different files with no incomplete dependency. Tasks that share a file (`pipeline.py`, T050–T055) are sequential.
- Every assertion in `tests/contract/` stays unchanged throughout (SC-001).
