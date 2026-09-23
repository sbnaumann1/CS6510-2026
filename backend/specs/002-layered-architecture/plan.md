# Implementation Plan: Layered Architecture Refactor

**Branch**: `002-layered-architecture` | **Date**: 2026-09-23 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/002-layered-architecture/spec.md`

## Summary

Re-cut the monolith's `app/api/*` → `app/services/*` → raw SQL structure into four explicit
layers — API, Transactions, Analytics, Database access — with no change to any API contract,
the database schema, or observable behavior (spec FR-007, FR-008). The database-access layer is
new: every `text(...)` SQL statement currently embedded in `services/transactions.py`,
`services/analytics.py`, and `services/inventory.py` moves into repository functions grouped by
the business layer that owns them. The two business layers absorb `services/inventory.py`'s two
responsibilities on either side of the read/write split the user specified: alert-crossing
emission (write, runs inside the completion invariant) goes to Transactions; the low-stock report
read (`GET /inventory/low-stock`) goes to Analytics, alongside popular-items, since both are
read-only reports over state Transactions produces. `background.py` splits along the same seam:
the abandonment sweeper moves under Transactions, the recompute scheduler under Analytics.
`catalog_cache.py`, `errors.py`, and `money.py` stay as shared infrastructure imported by any
layer. The existing contract/integration test suite, which drives the app over HTTP, is the
regression oracle — it is expected to pass unmodified at every step.

## Technical Context

**Language/Version**: Python 3.11 (`.python-version`), managed with `uv` — unchanged.

**Primary Dependencies**: FastAPI 0.104.1, SQLAlchemy 2.0.23 (async), asyncpg 0.29.0, pydantic
2.5.0, orjson 3.9.10 — unchanged. This is a structural refactor; no new dependency is needed to
extract functions into new modules.

**Storage**: PostgreSQL, same six tables (`catalog_item`, `inventory_stock`, `transaction`,
`transaction_item`, `low_stock_alert`, `popular_window_snapshot`). `app/models.py` is untouched
(spec FR-008).

**Testing**: The existing pytest + pytest-asyncio + httpx `AsyncClient` suite
(`tests/contract/*`, `tests/integration/*`, `tests/unit/*`), unmodified (spec FR-009). It exercises
the app over HTTP, so it is blind to which module a given SQL statement or business rule lives in
— that is what makes it a valid regression oracle for a purely structural change.

**Target Platform**: Unchanged — same host/OS as `001-checkout-backend`.

**Project Type**: Single backend web service; this feature only reorganizes `backend/app/`.

**Performance Goals**: No new goal. Success criterion SC-004 requires the refactored hot paths
(scan, complete) to stay within the latency ranges already measured and reported for the monolith
in `specs/001-checkout-backend/plan.md` (`START` p50 0.75 ms / p95 1.26 ms, `SCAN` p50 0.80 ms /
p95 1.36 ms, `COMPLETE` p50 2.30 ms / p95 4.22 ms at 10 stations). Function-call indirection from
the new layers is expected to be sub-microsecond next to a network round trip and is not expected
to move these numbers meaningfully; Phase 3 of implementation should re-run the load client to
confirm rather than assume this.

**Constraints**: The frozen OpenAPI contract (`backend/spec.yaml` / `contracts/openapi.yaml` from
001) does not change — every request/response shape, status code, and error body stays exactly as
graded. No behavior visible outside `backend/app/` may change.

**Scale/Scope**: Same as 001 — 2 000-item catalog, 10–200 stations. This plan touches only
`backend/app/` (12 existing modules reorganized into ~13 new ones across 4 layers); it does not
touch `scripts/`, `tests/`, `load-client/`, or the database schema.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unmodified speckit template (placeholder
principles, unratified). Status: **PASS (vacuous)** — same as 001.

Default engineering gates applied in its place, and their post-design status:

| Gate | Pre-Phase 0 | Post-Phase 1 |
| --- | --- | --- |
| Contract-first — the frozen OpenAPI contract is not touched to make the refactor easier | PASS | PASS — no request/response/error shape changes anywhere in this plan |
| Simplicity / YAGNI — four layers is what the user asked for, not an invented fifth (e.g. no separate "inventory" layer, no premature repository-pattern abstraction beyond one function per query) | PASS | PASS — `services/inventory.py` is split into the two layers that already need its two responsibilities, not kept as a fifth layer |
| Testability — every functional requirement has an automated check | PASS | PASS — spec FR-009's oracle is the unmodified existing suite; SC-002's "no raw SQL outside db layer" is a grep-able, scriptable check (see quickstart.md) |
| Observability — a maintainer can find the code for a given rule without cross-referencing SQL | PASS | PASS — this *is* the feature; SC-003 states it directly |

No follow-up beyond what 001 already recommended (ratifying real constitution principles).

## Project Structure

### Documentation (this feature)

```text
specs/002-layered-architecture/
├── plan.md              # This file
├── research.md          # Phase 0 output — module-boundary decisions
├── data-model.md        # Phase 1 output — the database-access layer's function contracts
├── quickstart.md        # Phase 1 output — how to verify the refactor
├── contracts/
│   └── layer-boundaries.md  # the internal contract this feature introduces: which layer may call which
├── checklists/
│   └── requirements.md
└── tasks.md              # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
backend/app/
├── main.py                    # app factory; wiring updated to import from new layer packages
├── config.py                  # unchanged
├── models.py                  # unchanged (FR-008: no schema change)
├── schemas.py                 # unchanged
├── errors.py                  # unchanged — shared kernel
├── money.py                   # unchanged — shared kernel
├── catalog_cache.py           # unchanged — shared infrastructure (not one of the four layers)
├── api/                       # LAYER: API
│   ├── _request.py            #   NEW — JSON-body / query-param helpers, moved out of
│   │                          #   transactions.py and inventory.py so routers don't import
│   │                          #   from each other
│   ├── catalog.py             #   unchanged (reads catalog_cache directly, no business layer)
│   ├── transactions.py        #   calls app.transactions instead of app.services.transactions
│   ├── inventory.py           #   calls app.analytics.low_stock instead of app.services.inventory
│   └── analytics.py           #   calls app.analytics.popular_items
├── transactions/               # LAYER: Transactions (replaces services/transactions.py +
│   │                           # the write half of services/inventory.py)
│   ├── __init__.py
│   ├── service.py              #   start/scan/complete/get, id parsing, response shaping,
│   │                           #   the completion invariant's transaction-boundary control
│   ├── alerts.py                #   emit_crossings — low-stock alert emission (write side)
│   └── sweeper.py               #   abandoned-transaction sweep (moved from background.py)
├── analytics/                  # LAYER: Analytics (replaces services/analytics.py + the
│   │                           # read half of services/inventory.py)
│   ├── __init__.py
│   ├── popular_items.py         #   recompute + read, response shaping
│   ├── low_stock.py             #   low_stock() read, response shaping
│   └── scheduler.py             #   should_recompute + recompute_window trigger (moved from
│                                 #   background.py)
├── db/                         # LAYER: Database access (new)
│   ├── __init__.py              #   re-exports engine, SessionLocal, get_session,
│   │                           #   advisory_lock — same public names as today's db.py
│   ├── transactions_repo.py     #   every text() statement from services/transactions.py,
│   │                           #   plus the low-stock alert INSERT from services/inventory.py
│   └── analytics_repo.py        #   every text() statement from services/analytics.py, plus
│                                 #   the low-stock SELECT from services/inventory.py
├── services/                    # DELETED once the above lands — nothing left to hold
└── background.py                 # DELETED — split between transactions/sweeper.py and
                                    # analytics/scheduler.py; main.py's lifespan calls both directly
```

**Structure Decision**: `app/db.py` becomes the `app/db/` package (`__init__.py` keeps engine,
session factory, and the advisory-lock helper under their current import path so nothing else
needs to change its imports of those three names) plus one repository module per business layer.
One repo module per layer — not one per current service file — because the layer, not the
originating file, is the seam this feature is defined around; `inventory.py`'s two queries
already belong on opposite sides of that seam. `app/services/` and `app/background.py` are
deleted rather than kept as re-export shims: nothing outside `backend/app/` imports them (they
are not part of the frozen contract), so there is no compatibility surface to preserve, and a
shim would just be a fifth, pointless layer.

## Complexity Tracking

*No constitution violations — table omitted.*
