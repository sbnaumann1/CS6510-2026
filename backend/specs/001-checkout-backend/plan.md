# Implementation Plan: Supermarket Self-Checkout Backend API

**Branch**: `001-checkout-backend` | **Date**: 2026-09-16 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-checkout-backend/spec.md`

## Summary

Implement the frozen self-checkout OpenAPI contract (`backend/spec.yaml`) as a single FastAPI
service backed by PostgreSQL, sized to survive the unmodified Java load client at 10 stations
(normal) and 100+ stations (stress) without ever overselling stock.

Technical approach: an async FastAPI app (asyncpg + SQLAlchemy 2.0) running as N stateless
uvicorn workers in front of one PostgreSQL instance. The immutable 2 000-item catalog is cached
in process memory (and `GET /items` is served as a pre-rendered JSON body), so the hot path is
one DB round trip per request. Scans are a single CTE statement (insert line + bump denormalized
counters, `RETURNING` the values the response needs). Completion is one DB transaction that locks
the affected `inventory_stock` rows in SKU order (`SELECT ... ORDER BY sku FOR UPDATE`), then
applies one conditional batched `UPDATE ... FROM (VALUES ...) WHERE current_stock >= qty`; if
fewer rows come back than SKUs in the basket, the whole transaction rolls back with
`409 INSUFFICIENT_STOCK`. That is what makes the graded invariant
(`initial_stock - final_stock == completed line items`, never negative) hold under concurrency.
Popular items use the `transaction_item` identity column as the global scan sequence, with a
hopping window recomputed out-of-band every 500 scans into a snapshot row, so all workers serve
identical, stable rankings.

## Technical Context

**Language/Version**: Python 3.11 (`.python-version`), managed with `uv`

**Primary Dependencies**: FastAPI 0.104.1, uvicorn[standard] 0.24.0 (uvloop + httptools),
SQLAlchemy 2.0.23 (async, ORM for schema / Core+`text()` on hot paths), asyncpg (added — see
[research.md](./research.md) R1), pydantic 2.5, orjson (added, R7), python-dotenv.
psycopg2-binary retained for sync seed/verify scripts only.

**Storage**: PostgreSQL 16/17, single instance, local Unix-domain socket (R3). Tables:
`catalog_item`, `inventory_stock`, `transaction`, `transaction_item`, `low_stock_alert`,
`popular_window_snapshot` — see [data-model.md](./data-model.md).

**Testing**: pytest + pytest-asyncio + httpx `AsyncClient` (contract + integration), plus two
harness scripts: `scripts/seed.py` (deterministic reset) and `scripts/verify_invariant.py`
(the grading correctness check). The Java load client in `load-client/` is the performance test.

**Target Platform**: macOS (Apple Silicon, 11 cores / 18 GB — the grading host) and Linux;
server listens on `http://localhost:8080` because the load client defaults there.

**Project Type**: Single backend web service (monolith week of the architecture series).

**Performance Goals**: The spec (SC-001, SC-002) requires latency and throughput to be measured
and reported for week-to-week comparison, not to clear a fixed bar. This plan adopts a stack-specific
target band to design against, validated in Phase 3 measurement: p50 ≤ 1.5 ms, p95 ≤ 4 ms,
p99 ≤ 10 ms per operation and ≥ 400 transactions/s at 10 stations; no error-rate regression and no
correctness regression at 100 stations. The band is set well below the host's mock-server reference
(`report-20260916-185335.json`: p50 0.259 ms, 2 624 tx/s) because that figure is in-process
`ConcurrentHashMap` state with no persistence. A Python event loop (~0.15–0.3 ms/request) plus a
PostgreSQL round trip (~0.08–0.15 ms on a Unix socket) cannot match it while durably persisting
every scan, and persistence is non-negotiable here — dropping to in-memory state would buy latency
at the cost of SC-004 and would make the monolith-vs-later-weeks comparison meaningless.

**Measured (Phase 3, this host, 4 workers / pool 12, `--http h11`)** — normal run, 10 stations /
60 s, seeded so nothing depletes (quickstart V7), 0 % errors on every operation:

| Operation | p50 | p95 | p99 | Band |
| --- | --- | --- | --- | --- |
| `START_TRANSACTION` | 0.75 ms | 1.26 ms | 1.94 ms | within |
| `SCAN_ITEM` | 0.80 ms | 1.36 ms | 2.06 ms | within |
| `COMPLETE_TRANSACTION` | 2.30 ms | 4.22 ms | 5.79 ms | p50/p95 over |

788 transactions/s and 8 239 items/s, comfortably past the ≥ 400 tx/s goal. `COMPLETE` sits above
the p50/p95 band and that is now understood rather than outstanding: the band was written as one
figure for all three operations, but completion is five round trips under row locks (lock the
transaction, group the basket, lock stock in SKU order, conditional batched decrement, finalize)
against one for a scan. The band is retained as written for week-to-week comparability; treat
≤ 1.5 ms / ≤ 4 ms as the scan-and-start target and ~2.5 ms / ~4.5 ms as the completion target.

Stress (100 stations / 120 s): `START` 6.14 / 12.95 / 20.74 ms, `SCAN` 6.24 / 12.98 / 20.55 ms,
`COMPLETE` 9.89 / 74.60 / 189.53 ms, 9 295 items/s, no transport errors, no timeouts, no 5xx, and
the graded invariant still passing on 225 431 units. Degraded but bounded, which is what SC-008
asks for.

Worker tuning: 8 workers measured 784.7 tx/s against 4 workers' 788.0 tx/s — no gain, slightly
worse tails. At 10 stations there are only 10 concurrent requests, so 4 workers already saturates
the useful parallelism. `WORKERS=4`, `DB_POOL_SIZE=12` retained (48 connections, well under
`max_connections = 200`).

**Constraints**: Contract is frozen — the load client is never modified, so response shapes,
status codes (201 on start, 200 elsewhere) and field names must match `spec.yaml` exactly,
including replacing FastAPI's default 422/`{"detail":...}` error shape with 400/`{error,message}`
(R8). Stock must never go negative and must never be silently clamped. All state persists in
PostgreSQL and survives a server restart.

**Scale/Scope**: 2 000 catalog items × 10 000 units initial stock; 10 stations (normal) to
200 stations (stress); baskets of 1–20 units; ~12.5 HTTP requests per transaction; a 60 s
default run produces on the order of 10⁵–10⁶ `transaction_item` rows.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unmodified speckit template — it contains
placeholder principles (`[PRINCIPLE_1_NAME]` …) and no ratified rules, so there are no
project-specific gates to enforce. Status: **PASS (vacuous)**.

Default engineering gates applied in their place, and their post-design status:

| Gate | Pre-Phase 0 | Post-Phase 1 |
| --- | --- | --- |
| Contract-first — implementation conforms to the frozen OpenAPI spec, spec is not edited to fit the code | PASS | PASS — contract copied read-only to `contracts/`, error catalog derived from it |
| Simplicity / YAGNI — no extra services, brokers or caches beyond what the requirements need | PASS | PASS — one process type + one database; no Redis, no broker |
| Testability — every functional requirement has an automated check | PASS | PASS — contract + integration tests, plus `verify_invariant.py` for SC-003/SC-004 |
| Observability — failures are diagnosable | PASS | PASS — structured error codes, `/transactions/{id}` debug endpoint, invariant script |

Recommended follow-up (not blocking): run `/speckit-constitution` to ratify real principles
before the next architecture week, so week-to-week comparisons are governed by stated rules.

## Project Structure

### Documentation (this feature)

```text
specs/001-checkout-backend/
├── plan.md              # This file
├── research.md          # Phase 0 output — 12 resolved decisions
├── data-model.md        # Phase 1 output — entities, DDL, indexes, invariants
├── quickstart.md        # Phase 1 output — provision, seed, run, load-test, verify
├── contracts/
│   ├── README.md            # How the frozen contract binds this implementation
│   ├── openapi.yaml         # Frozen copy of backend/spec.yaml (do not edit)
│   └── error-catalog.md     # error code ↔ HTTP status ↔ trigger, per endpoint
├── checklists/
│   └── requirements.md      # Existing spec-quality checklist
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── __init__.py
│   ├── main.py              # app factory, lifespan, exception handlers, router wiring
│   ├── config.py            # env-backed settings (catalog size, stock, thresholds, window, DSN)
│   ├── db.py                # async engine + session/pool wiring, advisory-lock helper
│   ├── models.py            # SQLAlchemy ORM models = schema source of truth
│   ├── schemas.py           # Pydantic request models + response models (docs/cold paths)
│   ├── catalog_cache.py     # in-memory SKU -> (name, price_cents) + pre-rendered /items body
│   ├── api/
│   │   ├── catalog.py       # GET /items
│   │   ├── transactions.py  # POST /transactions, /{id}/items, /{id}/complete, GET /{id}
│   │   ├── inventory.py     # GET /inventory/low-stock
│   │   └── analytics.py     # GET /analytics/popular-items
│   ├── services/
│   │   ├── transactions.py  # scan CTE, completion transaction (lock order + batched decrement)
│   │   ├── inventory.py     # low-stock query + alert emission
│   │   └── analytics.py     # hopping-window recompute + snapshot read
│   ├── background.py        # popular-window recompute trigger, abandoned-transaction sweeper
│   └── errors.py            # ApiError model, error codes, handlers (400/404/409)
├── scripts/
│   ├── seed.py              # create schema + load 2000 items + reset stock (idempotent)
│   ├── verify_invariant.py  # SC-003/SC-004 grading check against the live DB
│   └── run_server.sh        # uvicorn launch with tuned worker/loop/log flags
├── tests/
│   ├── conftest.py
│   ├── contract/            # one module per endpoint: status codes, field names, error bodies
│   ├── integration/         # US1 checkout flow, US2 low-stock, US3 window, concurrency race
│   └── unit/                # money math, window bookkeeping, config
├── docker-compose.yml       # fallback PostgreSQL (native Homebrew install preferred — R3)
├── .env.example
├── spec.yaml                # the frozen contract (already present)
└── pyproject.toml
```

**Structure Decision**: Single-project layout rooted at `backend/`, with a thin `api/` layer over
a `services/` layer over SQLAlchemy models. This is the monolith week, so everything runs in one
process type; the api/services split exists because later weeks (layered, service-based) will
re-cut the same domain along those seams, and keeping them visible now makes the week-to-week
comparison meaningful rather than a rewrite. `load-client/`, `mockserver/` and `spec/` at the
repository root are course-provided and are not modified.

## Complexity Tracking

> Deviations from the specification, and why they are necessary.

| Violation | Why Needed | Simpler Alternative Rejected Because |
| --- | --- | --- |
| **C1** — Raw SQL (`text()`/CTE) on the scan and completion paths instead of pure ORM unit-of-work | These two paths run ~12 times per transaction and carry the correctness-critical locking. ORM flush/identity-map overhead is a measurable share of a sub-2 ms budget, and the lock-ordering + conditional batched decrement have no clean ORM expression. | Pure-ORM was rejected on latency and on the clarity of the locking code. SQLAlchemy ORM models remain the single schema source of truth, and all cold paths use the ORM, so the "SQLAlchemy ORM" stack requirement holds. |
