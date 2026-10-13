# Implementation Plan: Service-Based Architecture (Domain Services over a Shared Database)

**Branch**: `005-service-architecture` | **Date**: 2026-10-13 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/005-service-architecture/spec.md`

## Summary

Split today's single FastAPI process (`app/`) into **four independently deployable domain
services** (Catalog, Checkout, Inventory, Analytics) plus an **nginx gateway** on `:8080`.
All of them run under **Docker Compose** with **one PostgreSQL** container and a one-shot
**seed** step.

The services never call each other. Their only coupling is a versioned shared library,
**`checkout-data`**. It holds the schema, connection handling, a **table-ownership
registry**, and **one repository module per consuming service**. An AST-based
**boundary check** enforces that coupling in the test suite. A second shared library,
**`checkout-web`**, carries the contract's error shape and request helpers, and has no
database access.

The one cross-domain runtime trigger left in the monolith goes away. Today a scan schedules
the analytics recompute in-process, which starts a brand-new pipeline run every 500 scans.
Instead, Analytics runs **one persistent streaming pipeline** for its whole lifetime. That
pipeline keeps spec 004's five filters and their queues. **Tail ingest** follows the shared
scan record as rows commit, using a contiguous watermark with a 1 s grace for out-of-order
commits. The **window filter** slides by scan id every 500. **Aggregation** keeps an
incremental per-SKU count, **ranking** breaks ties by SKU, and **output** commits each window
through a latest-wins slot. One leader holds the pipeline via a PostgreSQL advisory lock,
and a supervisor rebuilds the pipeline from the database after any error or restart.

The external contract, error catalog and graded invariant are unchanged. The two graded
load-client reports are produced by `scripts/run_load_tests.sh` against a fresh compose
stack.

## Technical Context

**Language/Version**: Python 3.11 (unchanged, `.python-version`).

**Primary Dependencies**:
- Unchanged: FastAPI 0.104, uvicorn 0.24 (uvloop, h11), SQLAlchemy 2.0 async + asyncpg 0.29, orjson, psycopg2-binary (seed and verify CLIs).
- New infrastructure: nginx 1.27 (gateway), Docker Compose v2, and a uv workspace for packaging (uv 0.9).

**Storage**: PostgreSQL 17, a single instance in the compose stack, shared by all services. The schema is unchanged from 001.

**Testing**: pytest + pytest-asyncio + httpx (unchanged). Two modes: in-process dispatcher (default) and gateway mode against the compose test stack (research R11). Plus the boundary check, the route-parity test, the lock-file freshness test and `check_independence.sh`.

**Target Platform**: Linux containers on Docker Desktop for macOS (Apple Silicon, 11 cores / 18 GB host; VM set to ≥ 8 CPU / ≥ 8 GB). The load client runs on the host.

**Project Type**: Multi-service web backend (monorepo, uv workspace).

**Performance Goals**:
- Both graded runs: zero transport, timeout or 5xx errors.
- Popular-items freshness ≤ 2 s after a slide boundary.
- Stress tx/s ≥ 80% of the spec 004 baseline (269.1 tx/s), or the gap is explained (SC-006).

**Constraints**:
- Frozen HTTP contract.
- Unmodified load client, using one base URL.
- No service-to-service calls.
- Total DB connections ≤ 75 of 200.
- `docker compose up -d --build --wait` is the only start command.

**Scale/Scope**:
- 2 000 SKUs.
- Default run: 10 stations / 60 s.
- Stress run: 100 stations / 120 s (~9 000 scans/s at peak, per spec 004).
- About 20 source modules moved, about 8 new files of infrastructure.

All Technical Context items are resolved. There are no `NEEDS CLARIFICATION` markers (see [research.md](./research.md)).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unmodified speckit template (placeholder
principles, unratified). Status: **PASS (vacuous)**, the same as 001–004.

The same default engineering gates used by 002 apply in its place:

| Gate | Pre-Phase 0 | Post-Phase 1 |
| --- | --- | --- |
| **Contract-first**: the frozen OpenAPI contract and error catalog are not changed to make the refactor easier | PASS | PASS. The gateway forwards unchanged, its default route preserves FastAPI's 404 body, and its outage body uses the existing `ApiError` shape ([gateway-routes.md](./contracts/gateway-routes.md)) |
| **Correctness invariant preserved**: the completion sequence stays one DB transaction | PASS | PASS. Stock decrement and alert insert stay in Checkout (research R1), and locks stay in the database (FR-015) |
| **No direct service coupling**: the assignment's rule | PASS | PASS. A persistent pipeline tailing the scan record replaces the in-process trigger (R5), and seven boundary rules are enforced by test (R12) |
| **Simplicity / YAGNI**: no machinery the assignment didn't ask for | PASS | PASS with justified additions (see Complexity Tracking). No message broker, no service discovery, no per-service DB roles |
| **Measurable**: every success criterion has a runnable check | PASS | PASS. Each SC maps to a quickstart step V1–V7 |

## Project Structure

### Documentation (this feature)

```text
specs/005-service-architecture/
├── spec.md
├── plan.md                         # this file
├── research.md                     # Phase 0: R1–R14
├── data-model.md                   # Phase 1: ownership registry, repo moves, persistent pipeline state, config
├── quickstart.md                   # Phase 1: V1–V8 validation guide
├── contracts/
│   ├── gateway-routes.md           # route table + outage behavior
│   ├── service-boundaries.md       # allowed/forbidden dependencies, boundary-check rules
│   └── compose-topology.md         # units, ports, health checks, commands
├── checklists/requirements.md
└── tasks.md                        # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (`backend/`)

```text
backend/
├── pyproject.toml                  # uv workspace root (virtual): members, dev deps, pytest config
├── uv.lock                         # one lock for the whole workspace
├── compose.yaml                    # replaces docker-compose.yml (research R7)
├── compose.test.yaml               # override: checkout_test DB, small catalog
├── packages/
│   ├── checkout_data/              # SHARED DATA-ACCESS LIBRARY — the only DB coupling point
│   │   ├── pyproject.toml          # name = "checkout-data", version = "0.5.0"
│   │   ├── Dockerfile              # "checkout-tools" image: seed + verify CLIs
│   │   └── src/checkout_data/
│   │       ├── __init__.py         # engine, SessionLocal, get_session, advisory_lock, dispose_engine
│   │       ├── config.py           # from app/config.py
│   │       ├── models.py           # from app/models.py (schema)
│   │       ├── money.py            # from app/money.py
│   │       ├── catalog.py          # CatalogCache (from app/catalog_cache.py, minus /items rendering)
│   │       ├── ownership.py        # TABLES registry (data-model §1)
│   │       ├── repos/{catalog,checkout,inventory,analytics}.py
│   │       ├── seed.py             # from scripts/seed.py, + --if-empty
│   │       └── verify_invariant.py # from scripts/verify_invariant.py
│   └── checkout_web/               # SHARED HTTP CONVENTIONS — no DB access
│       ├── pyproject.toml          # name = "checkout-web", version = "0.5.0"
│       └── src/checkout_web/
│           ├── errors.py           # from app/errors.py
│           ├── request.py          # from app/api/_request.py
│           ├── app.py              # create_service_app(name, routers, lifespan)
│           └── health.py           # GET /healthz
├── services/
│   ├── catalog/    {pyproject.toml, requirements.lock, Dockerfile, src/catalog_service/{main,api}.py}
│   ├── checkout/   {…, src/checkout_service/{main,api,service,alerts,sweeper}.py}
│   ├── inventory/  {…, src/inventory_service/{main,api,low_stock}.py}
│   └── analytics/  {…, src/analytics_service/{main,api,popular_items,pipeline,supervisor}.py}
│                   #   pipeline.py: persistent TailIngest/Window/Aggregation/Ranking/Output filters
│                   #   supervisor.py: leader lock, bootstrap, restart-on-error, shutdown
├── gateway/
│   ├── Dockerfile                  # FROM nginx:1.27-alpine
│   └── nginx.conf                  # contracts/gateway-routes.md
├── scripts/
│   ├── run_load_tests.sh           # rewritten for compose (research R14)
│   ├── check_boundaries.py         # new (research R12)
│   ├── check_independence.sh       # new (quickstart V6)
│   └── lock_services.sh            # new: uv export per service (research R3)
└── tests/
    ├── conftest.py                 # in-process dispatcher + gateway mode
    ├── contract/                   # unchanged assertions; imports updated
    ├── integration/                # + test_persistent_pipeline.py (windows == SQL oracle, no restart per slide),
                                    #   test_pipeline_restart.py (backfill after cancel)
    └── unit/                       # + test_boundaries.py, test_gateway_routes.py, test_service_locks.py,
                                    #   test_pipeline_filters.py (each filter via mock queues),
                                    #   test_tail_ingest_gaps.py (late commits, permanent gaps)
```

The old `app/` package, `run.sh`, `scripts/run_server.sh`, `scripts/seed.py`,
`scripts/verify_invariant.py` and `docker-compose.yml` are removed once their contents have
moved. `scripts/seed.py` and `scripts/verify_invariant.py` survive as thin host-side shims
that call the `checkout_data` modules, because `tests/conftest.py` invokes the seed on the
host.

**Structure Decision**: This is a monorepo uv workspace. The shared libraries live under
`packages/` and the deployable services under `services/`. Each service is its own Python
distribution with its own image (`services/<name>/Dockerfile`). The gateway has its own
image too (`gateway/`). The whole system is defined by `compose.yaml`.

## Phase 0 → Phase 1 traceability

| Spec requirement | Research | Design artifact |
| --- | --- | --- |
| FR-001 four services | R1 | data-model §1, service-boundaries |
| FR-002/003 independent units | R3, R7 | compose-topology (deps only on `seed`), quickstart V6 |
| FR-004 gateway | R4 | gateway-routes |
| FR-005/005a one command, health | R7, R13 | compose-topology, quickstart V1 |
| FR-006/007/008 contract, invariant, client | R4, R10, R11 | quickstart V3, V7 |
| FR-009/011/016 no direct coupling, ownership, check | R2, R12 | service-boundaries, data-model §1–2 |
| FR-010 one database | R8 | compose-topology |
| FR-012 analytics via DB | R5, R6 | data-model §3, quickstart V5 |
| FR-012a persistent pipeline, incremental windowing | R5 | data-model §3 (items, per-filter state, lifecycle) |
| FR-012b exact window contents, SKU tie-break | R5, R6 | data-model §3 invariants; `test_persistent_pipeline.py` |
| FR-013/014 Checkout/Inventory read from DB | R1, R2 | data-model §2 |
| FR-015 DB-level mutual exclusion | R5, R8 | data-model §3 |
| FR-017/018 reports, README | R9, R14 | quickstart V7 |

## Implementation order (input to /speckit-tasks)

1. **Workspace and shared libraries**: create `packages/checkout_data` and `packages/checkout_web` by moving code, add `ownership.py` and `seed --if-empty`, and keep the old `app/` working temporarily by importing from them.
2. **Services**: carve out the four service packages from `app/`, delete the scan route's recompute hook and `scheduler.py`, and add `/healthz`.
3. **Persistent pipeline**: rework `pipeline.py` into long-lived filters (R5): TailIngest with the gap rule (R6), id-based WindowFilter with watermarks, incremental AggregationFilter, ranking with the SKU tie-break, and a latest-wins OutputFilter that commits per window. Add `supervisor.py` (leader lock, bootstrap, restart, shutdown) and start it from the Analytics lifespan.
4. **Tests (in-process)**: update the conftest dispatcher and imports, add the filter, gap, persistent-pipeline and restart tests, the boundary check and the route-parity test. Delete `app/` and get the suite green.
5. **Containers**: write the Dockerfiles, the lock files, `nginx.conf`, `compose.yaml` and `compose.test.yaml`, and validate quickstart V1–V3.
6. **Operations**: write `check_independence.sh` (V6), rewrite `run_load_tests.sh` and validate V5.
7. **Deliverables**: run V7 for both graded reports, then update `README.md` (services list, packaging, reports, the comparability note) and `load-client/reports/README.md`.

## Complexity Tracking

| Addition | Why needed | Simpler alternative rejected because |
| --- | --- | --- |
| Gateway (a 5th container) | The unmodified load client has one `baseUrl`, but there are four services | Having one service proxy for the others creates direct service-to-service calls (R4) |
| Two shared packages, not one | Keeps the deliberate DB coupling point free of web-framework code, so "what does a schema change affect?" has a clean answer | One mixed package hides the DB coupling among unrelated helpers (R2) |
| Per-service exported lock files | Lets one service rebuild without the others (US2 scenario 3) | `uv sync` over the whole workspace rebuilds every image on any change (R3) |
| AST boundary check | Per-table write ownership can't be expressed as greps | 002-style greps only cover imports (R12) |
| Gap-aware tail (watermark + 1 s grace) | Concurrent scans commit out of id order; a naive `id > last_id` tail would silently drop scans | A fixed lag fails under load and stalls when idle; CDC/logical replication is heavy for one table (R6) |
| Pipeline supervisor + leader lock | A pipeline that never exits needs an owner for restart, and must run at most once across replicas | Run-per-slide needed neither, but it is exactly what the clarification rejected (R5) |
| `--if-empty` seed mode | Makes it safe for compose to re-run the one-shot seed when a single service is started independently | `--reset` on every start could wipe a live database (R7) |
