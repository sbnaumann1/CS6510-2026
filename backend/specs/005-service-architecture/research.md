# Research: Service-Based Architecture (005)

**Feature**: `005-service-architecture` | **Date**: 2026-10-13 | **Spec**: [spec.md](./spec.md)

Each entry: **Decision / Rationale / Alternatives considered**. Research items from 001–004
(R1–R14 of 001 in particular) still hold unless an entry below supersedes one by name.

Baseline to beat or explain (SC-006), spec 004 reports on this machine, native processes,
native PostgreSQL over a Unix socket:

| Run | Report | tx/s | items/s |
| --- | --- | --- | --- |
| default (10 stations / 60 s) | `load-client/reports/report-20261002-163856.json` | 320.9 | 7 562 |
| stress (100 stations / 120 s) | `load-client/reports/report-20261002-164059.json` | 269.1 | 9 078 |

---

## R1. Service decomposition and table ownership

**Decision**: There are four domain services:
- **Catalog**: `GET /items`.
- **Checkout**: the four `/transactions…` operations, plus the abandoned-transaction sweeper and low-stock alert emission.
- **Inventory**: `GET /inventory/low-stock`.
- **Analytics**: `GET /analytics/popular-items`, plus the popular-window poller and the spec 004 streaming pipeline.

Each table has exactly one writing owner (data-model.md §1):
- Checkout owns `transaction`, `transaction_item`, `inventory_stock` and `low_stock_alert`.
- Analytics owns `popular_window_snapshot`.
- Catalog owns `catalog_item`. Only the seed tool writes it.

**Rationale**:
- The completion sequence locks the transaction row, locks stock rows in SKU order, applies a conditional batched decrement and inserts the alert rows. All of that happens in **one** database transaction (001 R10). That atomicity is what makes the graded invariant hold. If an "Inventory" service owned the decrement, Checkout would have to call it synchronously and coordinate a distributed rollback. That is exactly the direct coupling the assignment forbids. So stock *writes* stay in Checkout, and Inventory is the read-side business function: reporting stock health.
- Catalog is separate from Checkout because "what is for sale" is a different business function from "selling it". Its endpoint is also the only one with zero per-request DB work (pre-rendered body).

**Alternatives considered**:
- *Three services (fold Inventory into Checkout)*: simpler, but the low-stock report is a reporting concern consumed by store staff, not the checkout station. Merging it would blur the domain split being graded.
- *Inventory owns `inventory_stock` and Checkout calls it at completion*: needs synchronous service-to-service calls and a two-phase protocol. Rejected.
- *Five services (separate Alerts)*: alert emission must commit atomically with the decrement (001 SC-005), so it cannot leave Checkout's transaction.

## R2. How shared database-access logic is packaged and distributed

**Decision**: Use a **uv workspace** with two shared library packages and four service packages:

| Package (dist / import) | Kind | Contents |
| --- | --- | --- |
| `checkout-data` / `checkout_data` | shared library, the **only DB coupling point** | settings, async engine and session factory, advisory-lock helper, ORM models (schema), money helpers, catalog reader, table-ownership registry, **one repository module per consuming service** (`repos/catalog.py`, `repos/checkout.py`, `repos/inventory.py`, `repos/analytics.py`), seed and invariant-check CLIs |
| `checkout-web` / `checkout_web` | shared library, HTTP conventions only, **no DB access** | `ApiError` and error codes, contract error handlers, request-parsing helpers, `create_service_app()` factory, `/healthz` router |
| `catalog-service` / `catalog_service` | service | app, router |
| `checkout-service` / `checkout_service` | service | app, router, business logic, alerts, sweeper |
| `inventory-service` / `inventory_service` | service | app, router, low-stock report |
| `analytics-service` / `analytics_service` | service | app, router, read path, pipeline, poller |

Both shared packages carry an explicit version (`0.5.0`). Each service image installs them as regular (non-editable) packages, so the shared library is literally *distributed* into each deployable unit.

**Rationale**:
- The assignment says coupling "effectively occurs at the database access layer" and asks for deliberate sharing. Putting every SQL statement in one versioned package, grouped **by consuming service**, makes the coupling visible and reviewable. A schema change touches `checkout_data`, bumps its version, and the dependency graph says exactly which services must be redeployed.
- Grouping repositories by service (rather than by table) means each service imports one module that is *its* data-access surface. The ownership registry (R12) then checks that surface.
- The HTTP conventions are split into `checkout_web` so the data library stays free of web-framework imports. This continues the 002 rule that the DB layer never raises `ApiError`.

**Alternatives considered**:
- *Copy the repo code into each service*: no shared code, but four copies of the schema drift silently. That is the worst kind of hidden coupling.
- *A "data service" that owns the database and exposes it over HTTP*: turns the database coupling into service-to-service calls on every request. It contradicts "services should not communicate directly", and adds a hop to the hot path.
- *One shared package holding both data and web helpers*: fewer packages, but it mixes the one deliberate coupling point with unrelated framework glue, so it's harder to reason about what a schema change affects.
- *Repositories grouped by table*: makes write-ownership violations easy to write by accident (any service can import `repos.inventory_stock.decrement`).

## R3. Deployable units: one image per service, independently rebuildable

**Decision**: Each service has its own `Dockerfile` (`services/<name>/Dockerfile`, build context `backend/`), built in two stages:

1. Install third-party dependencies from that service's own exported lock file, `services/<name>/requirements.lock`, produced by `uv export --package <name>-service --frozen --no-dev --no-emit-workspace --no-hashes`. This layer is cached.
2. Copy and install `packages/` (the shared libraries), then copy and install `services/<name>/` only.

The final image runs `uvicorn <name>_service.main:app` on container port 8000. `scripts/lock_services.sh` regenerates the four lock files, and a unit test fails if any of them is stale relative to `uv.lock`.

**Rationale**: The image copies only its own service directory, so a change confined to Analytics invalidates only the Analytics image's layers. `docker compose up -d --build` then recreates only that container (US2 scenario 3). A change to `packages/` invalidates every image, which is the documented and intended coupling. The per-service lock file keeps dependency versions identical across images, all derived from the one workspace `uv.lock`.

**Alternatives considered**:
- *`uv sync --package X` inside the image with the whole workspace copied in*: simplest Dockerfile. But any edit in any service changes the build context of every image, so all four rebuild. That undermines independent deployability.
- *One image with four entry points*: technically four processes, but one artifact. That isn't "its own deployable unit".
- *Simple wheels plus launch scripts, no containers*: ruled out by the 2026-10-13 clarification (Docker Compose only).

## R4. Gateway: nginx path router

**Decision**: Use `nginx:1.27-alpine` with a committed `gateway/nginx.conf`, built into its own image (`gateway/Dockerfile`). It publishes host port 8080. Route table: [contracts/gateway-routes.md](./contracts/gateway-routes.md).

Key settings:
- `upstream` blocks with `zone` and `server <svc>:8000 resolve;`, plus `resolver 127.0.0.11 valid=5s`. nginx then re-resolves a restarted container's new IP, and it starts even if a service name is briefly unresolvable. The `resolve` parameter is available in open-source nginx since 1.27.3.
- `keepalive 64` per upstream, with `proxy_http_version 1.1` and `proxy_set_header Connection ""`, so gateway↔service connections are reused.
- `access_log off` and `worker_processes auto`.
- `error_page 502 503 504` returns `503` with the contract's `ApiError` shape: `{"error":"INTERNAL_ERROR","message":"<service> service is unavailable."}`. An outage therefore never returns an HTML body.
- A default `location /` routes to Checkout. Unmatched paths then get the same FastAPI 404 body the monolith returned, instead of an nginx page.
- `GET /gateway/healthz` is answered by nginx itself and used by the compose health check.

**Rationale**: nginx is a mature, low-overhead reverse proxy, and its config is pure routing, so FR-004's "no business logic" is evident by inspection. It also strips the Java client's `Upgrade: h2c` on the way upstream (nginx does not forward `Upgrade` unless told to), which removes the 001 R14 hazard on proxied traffic.

**Alternatives considered**:
- *A Python (Starlette/httpx) gateway*: adds a second Python hop and an event-loop copy of every body on the hot path. It also tempts logic into the gateway.
- *Traefik / Caddy / HAProxy*: all viable. Traefik's label-driven config spreads routing across the compose file; HAProxy and Caddy offer nothing extra here. nginx wins on familiarity and a single readable config file.
- *No gateway (load client points at Checkout, which proxies the rest)*: makes Checkout talk to other services directly. Rejected.

## R5. Analytics trigger: poll the shared scan sequence

**Decision**: The Analytics Service runs one background poller per process, started in its lifespan. Every `POPULAR_POLL_MS` (default 250 ms) it reads `max(transaction_item.id)`, an index-only read on the primary key. It computes the latest slide boundary `b = max − (max mod 500)`. If `b` is greater than the last published `window_end`, it takes `pg_try_advisory_lock(POPULAR_RECOMPUTE_LOCK)` and runs the spec 004 streaming pipeline unchanged for the window `(b − 1000, b]`, then records `b`. On startup it seeds its last published value from `popular_window_snapshot.window_end` (0 if absent), so a restart catches up on its first tick (US2 scenario 2).

The in-process `should_recompute()` / `BackgroundTasks` hook in the scan route is **deleted**.

**Rationale**:
- The poller satisfies FR-012: there is no notification from Checkout, only an observation of data Checkout already writes.
- Worst-case staleness is one poll interval plus one pipeline run. The pipeline does ten 100-row batches plus one upsert, typically tens of milliseconds. That is well inside the 2 s bound (SC-005).
- Cost is ~4 trivial queries per second, independent of load.
- At stress rates (~9 000 scans/s), several boundaries pass per tick. Computing only the latest is the same effective behavior as today, where the advisory lock already makes overlapping triggers skip.

**Alternatives considered**:
- *PostgreSQL `LISTEN/NOTIFY` issued by Checkout on each boundary*: lower latency. But Checkout would then have to know that a consumer exists and which channel it listens on. That is messaging between services routed through the database, which the spec rules out of scope.
- *A database trigger on `transaction_item` that sends `NOTIFY`*: hides the coupling inside the schema, and adds work to the hottest insert.
- *Checkout calls Analytics over HTTP*: forbidden by FR-009.
- *Recompute on read*: puts a 1 000-row aggregation on the read path, and isn't the spec 004 pipeline.

## R6. Window end = the slide boundary, not "max at run time"

**Decision**: The pipeline entry point takes an explicit `end` (the boundary `b` from R5) instead of reading `max_scan_seq` itself, so published windows end on multiples of 500.

**Rationale**: A poller only observes boundaries after the fact, so "max at run time" would publish arbitrary end points. Using the boundary keeps the published window a true hopping window (001 spec: 1 000-item window, 500-scan slide), and makes `windowEnd − windowStart = 1000` hold exactly once past the first window. The 001 data-model note on sequence gaps still applies: ids are allocated before commit, so a few in-flight scans below `b` may be missed. That is the same tolerance the monolith had.

**Alternatives considered**: *Keep max-at-run-time*. Behaviorally permitted by the contract, which only requires `windowEnd − windowStart ≤ windowSize`, but less faithful and less testable.

## R7. Compose topology, startup ordering, health

**Decision**: `backend/compose.yaml` (replacing the old fallback `docker-compose.yml`) defines eight compose services: `postgres`, `seed` (one-shot), `catalog`, `checkout`, `inventory`, `analytics`, `gateway`, and `tools` (profile `tools`, for on-demand CLIs).

Ordering:
- `seed` waits for `postgres` to be `service_healthy` (`pg_isready`).
- Every domain service waits for `seed` to be `service_completed_successfully`.
- `gateway` waits for all four to be `service_healthy`.

`docker compose up -d --build --wait` therefore returns only when the whole system is ready (FR-005a). `seed` runs `python -m checkout_data.seed --if-empty`, a new flag: it creates the schema and seeds only when `catalog_item` is absent or empty. A fresh state therefore comes from `down -v`, as the spec requires, and an independent `docker compose up -d <service>` re-running the one-shot dependency can never wipe a live database. `--reset` remains available via the `tools` service. Full details: [contracts/compose-topology.md](./contracts/compose-topology.md).

**Rationale**: Startup ordering through `depends_on` conditions plus health checks is native to Compose v2 and needs no wait-for scripts. Services load the catalog cache at startup, so they must not start before seeding finishes. Otherwise they would cache an empty catalog: the failure the existing startup warning exists for.

**Alternatives considered**:
- *Seeding inside each service's startup*: four services racing to drop the schema.
- *Seeding from the host*: needs a host Python toolchain and a published DB port, and breaks "one command".

## R8. Database connectivity and connection budget

**Decision**:
- **Connection**: services reach PostgreSQL over TCP on the compose network: `DATABASE_URL=postgresql+asyncpg://checkout:checkout@postgres:5432/checkout`.
- **Tuning**: carried over from 001 R11: `synchronous_commit=off`, `wal_compression=on`, `checkpoint_timeout=15min`, `max_wal_size=8GB`, `max_connections=200`, `work_mem=32MB`. `shared_buffers` is reduced to `1GB`, since the dataset is a few MB and the VM's memory is shared.
- **Host access**: PostgreSQL is also published on `127.0.0.1:5433` for the host-side test suite, so it doesn't collide with a Homebrew instance on 5432.
- **Connection budget** (`WORKERS × DB_POOL_SIZE`):

| Service | WORKERS | DB_POOL_SIZE | Max conns |
| --- | --- | --- | --- |
| checkout | 6 | 10 | 60 |
| catalog | 2 | 2 | 4 |
| inventory | 1 | 4 | 4 |
| analytics | 1 | 4 | 4 (+1 advisory-lock conn) |
| seed / tools | — | — | ≤ 2 |
| **Total** | | | **≤ 75 of 200** |

**Rationale**: TCP between containers is the honest "distributed services" topology, and stays inside the Docker Desktop VM. Checkout is the only hot path, so it gets the workers. Analytics runs one worker so exactly one poller exists per container. The advisory lock still makes extra replicas safe (FR-015).

**Alternatives considered**: *Share the PostgreSQL Unix socket through a named volume mounted into every container*. It brings back 001 R3's no-TCP hot path and is a valid tuning lever if SC-006 falls short. It isn't the default because it ties all services to one host's filesystem.

## R9. Measurement environment and its risks

**Decision**:
- **Load client**: runs on the macOS host against `http://localhost:8080`, exactly as the assignment states. Everything else runs in Docker Desktop.
- **Docker Desktop resources**: before the measured runs, set them to **≥ 8 CPUs and ≥ 8 GB memory** (the host has 11 cores and 18 GB). Record the setting in the README next to the reports.
- **Comparability**: SC-006 compares against the spec 004 numbers above, and the README states why the two aren't strictly comparable.

**Rationale**: The two real costs added by this architecture are:
- the host→VM port-forwarding hop, which Docker Desktop's userland proxy adds to every request;
- the gateway hop, which adds one more proxied connection.

Both are visible in the numbers, and the README will name them rather than hide them.

**Alternatives considered**: *Run the load client in a container on the compose network*. That removes the host→VM hop, but it changes the assignment's stated invocation and `baseUrl`. It is kept as a diagnostic only, if the stress numbers need explaining.

## R10. Uvicorn HTTP parser

**Decision**: Keep `--http h11` and `--loop uvloop` for every service.

**Rationale**: Gateway traffic no longer carries `Upgrade: h2c` (R4). But services are also reachable directly on `127.0.0.1:8081–8084` for the US3 bypass test. h11 keeps them correct for any client, Java included. Switching to `httptools` is a measured tuning option, not a default.

## R11. Test strategy

**Decision**: There are two modes for the existing suite, plus new tests.

1. **In-process (default, `uv run pytest`)**: `tests/conftest.py` builds a test-only ASGI dispatcher. It mounts the four services' `create_app()` instances behind the same path table as `gateway/nginx.conf`. Contract and integration tests keep their assertions unchanged. Only their imports change, from `app.*` to the new packages. A unit test parses `nginx.conf` and asserts that its `location` table matches the dispatcher's, so the two cannot drift.
2. **Gateway mode (`GATEWAY_URL=http://localhost:8080 uv run pytest tests/contract`)**: the same contract tests run over real HTTP against the compose stack. The stack is started with the `compose.test.yaml` override, which sets `POSTGRES_DB=checkout_test`, `CATALOG_SIZE=50` and `STOCK_PER_ITEM=100`. The DB-reset fixture connects through `127.0.0.1:5433`. This is the check for SC-001.
3. **New tests**:
   - unit tests for the poller's boundary arithmetic;
   - an integration test that inserts 500 scans directly through Checkout's app and asserts the poller publishes a window, with no call into Analytics;
   - the boundary check (R12) as a test;
   - the lock-file freshness test (R3);
   - `scripts/check_independence.sh`, which stops `analytics`, asserts Catalog and Checkout still work and popular-items returns 503 in the error shape, then restarts it and asserts recovery (US2, SC-003).

**Alternatives considered**: *Only test through the real stack*. Slower and Docker-dependent for every edit. *Only in-process*. It never exercises the gateway, health checks or separate processes.

## R12. Boundary check (replaces 002's greps)

**Decision**: `scripts/check_boundaries.py` uses Python's `ast` (no new dependency) and runs as `tests/unit/test_boundaries.py`. It fails on any of:

1. A module under `services/<A>/` importing a module from `<B>_service`.
2. A service importing an HTTP client or socket module: `httpx`, `requests`, `aiohttp`, `urllib.request`, `http.client` or `socket`.
3. A service importing `checkout_data.repos.<X>` for any `X` other than its own name and `catalog`. `catalog` is the shared read-only module.
4. A service containing SQL (`sqlalchemy.text`, `select`, `insert`, `update`, `delete`). SQL lives only in `checkout_data`.
5. A `checkout_data/repos/<svc>.py` SQL string that writes (`INSERT INTO`, `UPDATE`, `DELETE FROM`) a table whose registered owner isn't `<svc>`, or reads (`FROM`, `JOIN`) a table whose registered readers don't include `<svc>`. The registry is `checkout_data.ownership.TABLES`.
6. A compose service's `environment` naming another domain service's hostname. Services are configured only with `DATABASE_URL` and tuning values.

The seed and verify CLIs in `checkout_data` are exempt from rule 5. They are operator tools that own the schema.

**Rationale**: Greps (002 R6) can't express per-table ownership. An AST check is precise, has no dependencies, and runs in milliseconds as part of the normal suite.

**Alternatives considered**: *import-linter*: handles rules 1 and 3 but not SQL ownership, and adds a dependency. *Database roles per service with table GRANTs*: the strongest enforcement, and a noted follow-up, but it adds four roles and role-specific URLs to manage for a one-week refactor. The static check catches the same mistakes at test time.

## R13. Health endpoints

**Decision**:
- Each service mounts `GET /healthz` from `checkout_web`. It returns `200 {"status":"ok","service":"<name>"}` after one `SELECT 1`. Catalog, Checkout and Analytics also require a non-empty catalog cache.
- Compose health checks call it with the container's own Python: `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"`. The slim image has no `curl`. This is compose config, not service code, so rule 2 of R12 doesn't apply.
- `/healthz` is **not** routed by the gateway. It isn't part of the frozen contract.

## R14. Load runs and where reports land

**Decision**: `scripts/run_load_tests.sh [default|stress|all]` is rewritten for compose. Per run:

1. `docker compose down -v`, then `docker compose up -d --build --wait`.
2. Build and run the load client from `../load-client`:
   - default: `./run.sh`, with no flags;
   - stress: `./run.sh --stations=100 --duration=120`.
3. Move the newly written `report-*.json` into `load-client/reports/005-service-architecture/`.
4. Save `docker compose logs` to `backend/logs/compose-<run>.log`. That file is gitignored.
5. Run `docker compose run --rm tools python -m checkout_data.verify_invariant`.
6. Print the same summary as today.

`load-client/reports/README.md` gets a Week 5 table: tx/s, items/s and COMPLETE error %, plus invariant results, Docker Desktop resources, and the spec 004 baseline for comparison.

**Rationale**: The load-client invocations match the assignment's wording exactly. Moving the file afterwards, rather than passing `--reportDir`, keeps the commands identical to what the grader expects.
