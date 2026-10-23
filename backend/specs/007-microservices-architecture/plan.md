# Implementation Plan: Microservices Architecture (Database per Service, Coordinated Cross-Service Updates)

**Branch**: `007-microservices-architecture` | **Date**: 2026-10-21 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/007-microservices-architecture/spec.md`

## Summary

Spec 005's four domain services stop sharing one PostgreSQL database and the `checkout_data`
library. Checkout and Inventory each get **their own PostgreSQL instance on its own private network**, Catalog keeps its fixed list in **a read-only file only it can open**, and Analytics keeps **no persistent store at all**: its window is derived state held in memory and rebuilt from Checkout's scans. Each service has its own data-access code. The frozen contract, the nginx gateway's public routes and the unmodified load
client do not change.

Three things were free under a shared database and now need design:

1. **Completing a transaction** touches Checkout and Inventory. It becomes an **orchestrated
   saga** driven by Checkout: mark `COMPLETING` → Inventory `reserve` (idempotent, one local
   transaction) → mark `COMPLETED`, with a `release` compensation, a durable `completion`
   record, and a recovery loop. See [research R1–R4](./research.md) and
   [contracts/completion-saga.md](./contracts/completion-saga.md).
2. **Feeding Analytics** is a **pull**: Analytics polls Checkout's `GET /internal/scans` for new
   scans and keeps its sliding window in memory, so it needs no database, no relay and no leader
   election. Analytics was already driven by scans, not sales, so the contract tests require this.
   See [research R5](./research.md).
3. **Needing another service's reference data** (prices, item names) uses Catalog's *public*
   `GET /items`, cached in memory as a snapshot. See [research R6](./research.md).

Cross-service calls are plain HTTP/JSON on a private `mesh` network with timeouts, bounded
retries and a propagated request id. No broker is introduced ([research R7](./research.md)).

## Technical Context

**Language/Version**: Python 3.11 (unchanged)

**Primary Dependencies**: FastAPI + uvicorn (uvloop, httptools), SQLAlchemy 2 async + asyncpg, orjson (all unchanged); **httpx** added to every service image for service-to-service calls; nginx for the gateway (unchanged)

**Storage**: PostgreSQL 17, **two instances**: `checkout-db`, `inventory-db` (one schema, one role, one private network each); Catalog: a read-only `catalog.csv` on a volume mounted only into Catalog and its seed job; Analytics: none (in-memory derived state)

**Testing**: pytest + pytest-asyncio against native PostgreSQL (two `*_test` databases plus a temporary catalog file); in-process service apps joined by a fault-injecting ASGI transport; compose-level failure drills; the graded load client

**Target Platform**: Docker Compose on Docker Desktop (macOS), as in 005

**Project Type**: multi-service web backend (four services + gateway + per-service one-shot seeds)

**Performance Goals**: stress run (100 stations, 120 s) ≥ 70 % of 005's throughput, with the shortfall explained if missed (SC-009); popular-items catches up within 30 s of Analytics recovering (SC-006)

**Constraints**: contract unchanged; stock never negative; no cross-database transactions; no service ever connects to another's database; every cross-service call has a deadline

**Scale/Scope**: 4 services, 2 databases, 1 file store and 1 in-memory read model, 1 cross-service write flow (completion), 1 async pull (scans), 2000-item catalog; load client up to 100 stations

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template, so there are no ratified
principles to gate against. In place of it, this plan holds itself to the standards the earlier
specs recorded:

| Standard (source) | Status |
| --- | --- |
| Frozen external contract and unmodified load client (001, 005 FR-001) | PASS: public routes unchanged; internal routes are not routed by the gateway |
| Graded invariant: `initial_stock − final_stock == completed line items`, never negative (001) | PASS: kept; verified across stores by an HTTP-based checker ([R10](./research.md)) |
| Boundary rules enforced by an automated check (005) | PASS: rewritten for database-per-service ([contracts/service-boundaries.md](./contracts/service-boundaries.md)) |
| Spec 006 hot-SKU finding: never hold the hot stock lock across client round trips | PASS: Inventory holds it for one statement plus COMMIT, then releases it before Checkout's finalize ([R2](./research.md)) |

Post-design re-check: PASS. No violations, so Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/007-microservices-architecture/
├── plan.md                          # This file
├── research.md                      # Phase 0: decisions with rationale and alternatives
├── data-model.md                    # Phase 1: per-service schemas and state machines
├── quickstart.md                    # Phase 1: validation scenarios
├── contracts/
│   ├── internal-apis.md             # service-to-service HTTP contracts
│   ├── completion-saga.md           # steps, states, compensation, recovery
│   ├── gateway-routes.md            # public routes, blocked /internal, request id
│   ├── compose-topology.md          # units, networks, credentials, startup order
│   └── service-boundaries.md        # rules enforced by scripts/check_boundaries.py
├── checklists/requirements.md
└── tasks.md                         # Phase 2 (/speckit-tasks, not created here)
```

### Source Code (backend/)

```text
backend/
├── compose.yaml                     # 2 DBs + catalog volume, 3 seeds, 4 services, gateway, tools; networks per topology contract
├── compose.test.yaml
├── gateway/                         # nginx.conf: + /internal/ blocked, + X-Request-ID
├── packages/
│   └── checkout_web/                # KEPT, data-free only: errors, request parsing, app factory,
│                                    #   /healthz, money helpers, NEW client.py (timeouts, retries,
│                                    #   request-id propagation). checkout_data is DELETED.
├── services/
│   ├── catalog/src/catalog_service/
│   │   ├── main.py  api.py
│   │   ├── config.py  store.py  seed.py              # reads/writes catalog.csv; no database, no SQLAlchemy
│   ├── checkout/src/checkout_service/
│   │   ├── main.py  api.py  service.py
│   │   ├── db.py  models.py  repo.py  seed.py
│   │   ├── saga.py                  # completion orchestrator + compensation
│   │   ├── recovery.py              # resumes/compensates stuck completions
│   │   ├── internal_api.py          # GET /internal/scans (read by Analytics) and /internal/reports/*
│   │   ├── catalog_client.py  inventory_client.py
│   │   └── sweeper.py
│   ├── inventory/src/inventory_service/
│   │   ├── main.py  api.py  low_stock.py
│   │   ├── reserve.py               # idempotent reserve/release statements
│   │   ├── db.py  models.py  repo.py  seed.py
│   │   └── catalog_client.py
│   └── analytics/src/analytics_service/
│       ├── main.py  api.py  popular_items.py  supervisor.py  pipeline.py   # pipeline reused from 005, no advisory lock
│       ├── config.py  scan_source.py   # HTTP ScanSource: reads Checkout's GET /internal/scans
│       ├── scan_window.py           # in-memory rolling window of recent scans
│       ├── internal_reports.py      # GET /internal/reports/cursor
│       └── catalog_client.py        # no database, no SQLAlchemy, no seed
├── tools/
│   ├── verify_invariant.py          # cross-store check over /internal/reports/*; opens no database
│   └── Dockerfile                   # the checkout-tools image (httpx only)
├── scripts/
│   ├── check_boundaries.py          # the eleven rules
│   ├── check_isolation.sh           # no service can reach another's store (from inside the containers)
│   ├── check_independence.sh        # recreate, then stop, each service; only dependants may fail
│   ├── check_crash_recovery.sh      # kill Checkout under load; the stores must agree afterwards
│   ├── trace_request.sh             # one request id across every service's log
│   ├── step_timings.py              # per-step timings from the logs
│   ├── verify_invariant.py          # host entry for tools/verify_invariant.py
│   ├── run_load_tests.sh            # REPORT_DIR default 007-microservices-architecture
│   └── lock_services.sh             # one lock per service; none contains another service's package
└── tests/
    ├── contract/                    # assertions unchanged
    ├── integration/                 # existing + saga, recovery, Analytics pull and rebuild
    ├── failure/                     # fault-injection per saga step (FR-023)
    ├── unit/
    ├── _dispatch.py                 # gateway twin + inter-service ASGI mesh with fault hooks
    └── conftest.py                  # two databases and a temp catalog file instead of one database
```

**Structure Decision**: Keep 005's layout (one directory per service, shared `checkout_web` for
cross-cutting code only) and replace the shared data library with a data layer inside each
service. Each service remains an independent installable package with its own lock file and
image. This is the smallest change that satisfies FR-005 and FR-006.

## Complexity Tracking

No constitution violations to justify. One choice adds moving parts, required by the spec:

| Addition | Why Needed | Simpler Alternative Rejected Because |
| --- | --- | --- |
| `completion` table and recovery loop in Checkout | FR-015: a crash mid-saga must not strand a transaction | Relying on the client to retry leaves stock reserved for abandoned shoppers |
