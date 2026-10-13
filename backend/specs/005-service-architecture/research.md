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
- **Analytics**: `GET /analytics/popular-items`, plus the persistent popular-items streaming pipeline (R5).

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
| `analytics-service` / `analytics_service` | service | app, router, read path, persistent pipeline (filters), pipeline supervisor |

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

## R5. Analytics: one persistent streaming pipeline that tails the scan record

**Decision**: The Analytics Service runs **one long-lived pipeline** for the life of the
process, started in its lifespan. It keeps spec 004's five filters and their queue
connections. Every filter is a `while True` loop that only exits on shutdown. There is no
per-slide run, no `asyncio.gather` per window, and no `TERMINATE` between windows.

```text
TailIngest ──q1──▶ WindowFilter ──q2──▶ AggregationFilter ──q3──▶ RankingFilter ──q4──▶ OutputFilter
 (DB tail)          (deque, slide)       (incremental Counter)      (top-200, ties by sku)   (latest-wins, commit per window)
```

| Filter | Spec 004 behavior (per run) | 005 behavior (persistent) |
| --- | --- | --- |
| **TailIngest** (was IngestFilter) | read a fixed range `(end−1000, end]` once, then `TERMINATE` | Loop forever: `get_scans_after(last_id, limit=1000)`, emit `ScanData` in id order, advance `last_id` with the gap rule (R6). Sleep `POPULAR_POLL_MS` (100 ms) only when a batch comes back empty or short. A full batch loops immediately, so a backlog drains at DB speed. |
| **WindowFilter** | buffer up to 1000, emit every 500 *received* | Window is defined by **scan id**, not arrival count. It keeps `deque` entries with `id > b − 1000`. When ingest passes the next boundary `b` (a multiple of 500; signalled by a `Watermark(b)` item, R6), it evicts ids `≤ b − 1000` and emits a `Slide(b, added, evicted)` delta. |
| **AggregationFilter** | `Counter` over all 1000 scans per window | Maintains **one** running `Counter`. Each `Slide` applies `+1` per added scan and `−1` per evicted one (dropping zeros), then emits `WindowCounts(start=b−1000, end=b, counts)`. That is O(500) work per slide instead of O(1000). |
| **RankingFilter** | collect, sort by count only | Sort by `(−count, sku)` (FR-012b; same order as `window_counts` SQL), take the top 200, emit `RankedData`. |
| **OutputFilter** | one upsert, commit only at `TERMINATE` | **Commit per window** (a persistent pipeline never reaches `TERMINATE`). Its input `q4` is **latest-wins**: a size-1 slot where a newer window replaces an unwritten older one. A burst of slides then costs one DB write, not a backlog (spec edge case). |

Queues `q1`–`q3` are bounded (`maxsize` 10 000 / 64 / 64), so a slow downstream filter
back-pressures the tail instead of growing memory. Window state is about 1 000 `ScanData`
plus at most 2 000 counter entries.

**Bootstrap / restart**: on start, the pipeline reads `max_seq`, sets
`b₀ = max_seq − (max_seq mod 500)`, and starts the tail at `last_id = max(0, b₀ − 1000)`. The
backfill is therefore the same tail code, not a special path. The first emitted window is
`b₀`, followed by live windows. Below the first boundary, the existing on-demand read path
answers (unchanged).

**Leadership (FR-015)**: the pipeline runs only while its process holds the session-level
advisory lock `POPULAR_RECOMPUTE_LOCK` on a dedicated connection. A process that fails to get
it retries every 2 s as a standby. If the leader dies, PostgreSQL releases the lock with
the connection, and a standby bootstraps as above. With the planned one Analytics replica,
the lock is simply always held.

**Supervision**: a supervisor task wraps the pipeline. Any filter exception is logged,
the whole pipeline is cancelled (all filters), and it re-bootstraps after a 1 s back-off. A
broken pipeline therefore never sits half-alive, and the service stays up (004 FR11).

**Observability (004 FR9 kept)**: the supervisor logs `pipeline started bootstrap_from=<id>` once per bootstrap and `pipeline restarting after error`. OutputFilter logs `window published start=<s> end=<e>` at info level. The quickstart (V5) greps these exact phrases. The Analytics service sets its own logger to INFO, independently of uvicorn's `--log-level warning`.

**Shutdown**: lifespan exit puts `TERMINATE` into `q1`. Filters forward it and exit, and the
lock connection is closed.

**Rationale**:
- This is what the user asked for: one pipeline that accepts scans as they arrive and owns the windowing. It is also the purer pipes-and-filters design. Each filter keeps state across windows, which spec 004's run-per-slide design couldn't do (its filters were rebuilt from scratch every 500 scans).
- Detection still satisfies FR-012: Analytics observes rows that Checkout already writes, and Checkout never knows Analytics exists.
- **Freshness**: one poll interval (≤ 100 ms) plus one slide through four in-memory filters plus one upsert. That's tens of milliseconds, well inside the 2 s bound (SC-005).
- **DB cost**: about 10 trivial queries/s when idle. Under stress (~9 000 scans/s) it's about 9 batched reads/s of ≤ 1 000 rows, plus at most one snapshot upsert per output cycle.

**Alternatives considered**:
- *Keep spec 004's run-per-slide pipeline, triggered by a poller*: this was the previous plan. Rejected by the 2026-10-13 clarification, because it rebuilds the window from the DB every 500 scans and the "pipeline" never persists.
- *`LISTEN/NOTIFY` push from Checkout instead of a tail*: lower idle cost, but Checkout would have to know a consumer exists (out of scope, spec), and NOTIFY payloads are lost while Analytics is down. The tail recovers from the table itself.
- *`LISTEN` as a wake-up hint on top of polling* (pre-push review, 2026-10-13): **rejected after measuring.** Polling already gives a scan-to-visible latency of 12 / 54 / 84 ms (min / median / max over 8 trials) against the 2 s target, at about 10 trivial queries/s when idle. A hint would need a `NOTIFY` sender. If Checkout sent it, Checkout would know about Analytics (FR-009). If a trigger sent it, every scan insert, the hottest statement in the system, would pay for it. Either way the most it could save is one 100 ms poll interval. Evidence: `checklists/pre-push-review.md` §2.
- *Logical replication / CDC (wal2json, Debezium)*: a true change stream, but it needs a replication slot, WAL configuration and a new dependency for a one-table tail. It's the right move at larger scale, and overkill here.
- *Window by arrival count (spec 004 WindowFilter)*: on restart, the arrival order differs from the id order, so windows wouldn't be reproducible. Windowing by id makes every published window equal to a direct SQL count over the same bounds (FR-012b), which is testable.

## R6. Tailing a sequence safely: out-of-order commits and gaps

**Problem**: `transaction_item.id` values are allocated at insert time, but concurrent scan
transactions commit in any order. A naive `id > last_id` tail that advances to the highest id
seen will **permanently skip** a lower id that commits a few milliseconds later. At 100
stations this happens constantly. Separately, some ids never appear: a sequence value
consumed by a statement that then rolled back is a permanent gap.

**Decision**: TailIngest keeps a *contiguous watermark* `last_id` and a small *pending* set:

1. Each poll reads `id > last_id ORDER BY id LIMIT 1000`.
2. Rows are accepted in order while `id == last_id + 1`, and `last_id` advances.
3. At the first hole (`id > last_id + 1`), rows above the hole are held in *pending*, and the hole's first-seen time is recorded.
4. The next poll re-reads from `last_id`. If the hole has filled, it continues contiguously.
5. If a hole stays unfilled for longer than `POPULAR_GAP_GRACE_MS` (default 1 000 ms), it is declared a permanent gap. The tail logs `gap skipped id=<n>` at INFO, skips it, and proceeds. It's INFO, not DEBUG, so the pre-push review (`checklists/pre-push-review.md`) can count and cross-check every skipped id.
6. After accepting rows, TailIngest emits `Watermark(b)` for every boundary `b` (a multiple of 500) with `b ≤ last_id` that hasn't been emitted yet. WindowFilter slides only on a watermark, so a window is emitted only once every id inside it has either arrived or been declared a gap.

**Rationale**:
- A scan commits within milliseconds of getting its id: the scan CTE is one statement in its own transaction. So 1 s of grace is several hundred times the expected delay. In practice it catches every late commit, at a worst-case freshness cost of 1 s (still inside SC-005's 2 s).
- Permanent gaps are rare. The scan CTE only consumes an id when the insert actually happens, so a gap needs a crash or rollback after the insert. They cost at most one grace period each.
- Window contents then exactly match `SELECT … WHERE id > b−1000 AND id <= b` evaluated after the fact (FR-012b). The integration test checks exactly that.

**Alternatives considered**:
- *Tail by `max(id)` minus a fixed lag (e.g. "only read ids ≤ max − 200")*: under load the lag is too small; when idle it delays the last scans forever.
- *Tail by a commit timestamp column*: needs a schema change (the schema is frozen by 001), and clock ties need their own tie-break anyway.
- *`txid_snapshot_xmin`-based visibility tracking*: correct, but much more subtle than a grace window for the same result here.

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

**Rationale**: TCP between containers is the honest "distributed services" topology, and stays inside the Docker Desktop VM. Checkout is the only hot path, so it gets the workers. Analytics runs one worker so exactly one pipeline candidate exists per container; the leader lock (R5) decides which candidate runs. The advisory lock still makes extra replicas safe (FR-015).

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
   - unit tests for each persistent filter fed through mock queues (004 SC3 carried over):
     - WindowFilter slides only on `Watermark` items and evicts by id.
     - AggregationFilter's incremental counts equal a fresh `Counter` after any sequence of slides.
     - RankingFilter breaks ties by SKU.
     - OutputFilter's latest-wins slot drops superseded windows and commits per window.
   - unit tests for TailIngest's gap rule, using a fake repo that returns out-of-order and gapped id sets: a late commit inside the grace period is never skipped, and a permanent gap is skipped after the grace period;
   - an integration test against PostgreSQL that runs the persistent pipeline as a task. It drives 1 600 scans through Checkout's app from concurrent tasks, then asserts:
     - windows ending at 500, 1 000 and 1 500 are published, each equal to a direct `window_counts` SQL query over the same bounds (FR-012b);
     - there were no calls into Analytics;
     - the pipeline task is the same object throughout, so it was never restarted per slide (FR-012a);
   - a restart test: cancel the pipeline mid-stream, start a new one, and assert it backfills and publishes the correct latest window;
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
