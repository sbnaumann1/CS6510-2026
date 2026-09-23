---

description: "Task list for 002-layered-architecture"
---

# Tasks: Layered Architecture Refactor

**Input**: Design documents from `/specs/002-layered-architecture/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/layer-boundaries.md](./contracts/layer-boundaries.md), [quickstart.md](./quickstart.md)

**Tests**: No new test tasks. spec.md's Assumptions state the existing `tests/contract/*` and `tests/integration/*` suite (which drives the app over HTTP and is blind to internal module boundaries) is the authoritative regression oracle for this feature (FR-009, SC-001); new layer-scoped unit tests are called out as a non-blocking follow-on, not part of this task list. Every phase below ends with a checkpoint task that runs the existing suite plus the grep checks from contracts/layer-boundaries.md.

**Organization**: Tasks are grouped by user story (spec.md P1/P2/P3) to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

Single project rooted at `backend/` (unchanged from 001). All paths below are relative to `backend/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the target package skeleton before any logic moves into it

- [X] T001 Convert `app/db.py` into `app/db/__init__.py` (pure file move, no logic change): the module content — `engine`, `SessionLocal`, `get_session`, `advisory_lock`, `dispose_engine`, `POPULAR_RECOMPUTE_LOCK` — is unchanged, only its path becomes a package so `app/db/transactions_repo.py` and `app/db/analytics_repo.py` (Phase 3) have somewhere to live; every existing `from app.db import ...` call site keeps working unmodified
- [X] T002 [P] Create `app/transactions/__init__.py` (empty package init)
- [X] T003 [P] Create `app/analytics/__init__.py` (empty package init)
- [X] T004 Run `uv run pytest` to confirm the T001 package conversion is behavior-preserving before any repository code is added

**Checkpoint**: Target package layout exists; nothing has moved yet; full suite green

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Remove the one existing cross-router import before the routers are rewired story-by-story, so US2 and US3 aren't each separately untangling it

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T005 Create `app/api/_request.py` containing `_json_body` and `_required_str` (moved verbatim from `app/api/transactions.py` lines 23–44) and `positive_int_param` (moved verbatim from `app/api/inventory.py` lines 17–29)
- [X] T006 [P] Update `app/api/transactions.py` to import `_json_body`/`_required_str` from `app.api._request` and delete its local definitions
- [X] T007 [P] Update `app/api/inventory.py` to import `positive_int_param` from `app.api._request` and delete its local definition; update `app/api/analytics.py` to import `positive_int_param` from `app.api._request` instead of from `app.api.inventory` (removing the current sideways router-to-router import)
- [X] T008 Run `uv run pytest` to confirm the helper extraction is behavior-preserving

**Checkpoint**: No API router imports from another API router; foundation ready for the database-access extraction

---

## Phase 3: User Story 1 - Isolate all raw SQL behind a database-access layer (Priority: P1) 🎯 MVP

**Goal**: Every SQL statement in `app/services/transactions.py`, `app/services/analytics.py`, `app/services/inventory.py`, and `app/background.py` is replaced by a call to a named repository function in `app/db/`; no business file contains a `text(...)` literal.

**Independent Test**: `uv run pytest` passes unchanged, and `grep -rn "text(" app/services app/background.py` (adjusted to the pre-move file locations this story operates on) prints nothing.

### Implementation for User Story 1

- [X] T009 [P] [US1] Create `app/db/transactions_repo.py` with `insert_transaction(session, station_id)`, `scan_item(session, tx_id, sku, price_cents)`, `get_status(session, tx_id)`, `lock_transaction(session, tx_id)`, `get_basket(session, tx_id)`, `lock_stock(session, skus)`, `decrement_stock(session, basket, tx_id)`, `complete_transaction(session, tx_id, total_cents)`, `get_transaction(session, tx_id)` — moving the SQL from `app/services/transactions.py`'s `_INSERT_TX`, `_SCAN`, `_TX_STATUS`, `_LOCK_TX`, `_BASKET`, `_LOCK_STOCK`, the inline `UPDATE ... FROM (VALUES ...)` decrement, `_COMPLETE_TX`, and `_GET_TX` respectively (signatures and return shapes per data-model.md's `transactions_repo.py` table); no function calls `commit`/`rollback`/`begin`
- [X] T010 [P] [US1] Add `insert_alerts(session, crossings)` to `app/db/transactions_repo.py`, moving the `_INSERT_ALERT` statement from `app/services/inventory.py`
- [X] T011 [P] [US1] Add `sweep_abandoned(session, minutes)` to `app/db/transactions_repo.py`, moving the inline `UPDATE transaction SET status = 'CANCELLED' WHERE status = 'OPEN' AND started_at < now() - make_interval(mins => :mins)` statement out of `app/background.py`'s `sweep_abandoned` function, returning the rowcount
- [X] T012 [P] [US1] Create `app/db/analytics_repo.py` with `max_scan_seq(session)`, `window_counts(session, start, end, depth)`, `upsert_snapshot(session, window_size, slide_interval, start, end, ranking_json)`, `read_snapshot(session)` — moving `_MAX_SEQ`, `_WINDOW_COUNTS`, `_UPSERT`, `_READ` from `app/services/analytics.py` (signatures per data-model.md's `analytics_repo.py` table)
- [X] T013 [P] [US1] Add `low_stock_report(session, threshold)` and `now(session)` to `app/db/analytics_repo.py`, moving the `_LOW_STOCK` statement and the `SELECT now()` (`_NOW`) statement from `app/services/inventory.py`
- [X] T014 [US1] Update `app/services/transactions.py` to call the T009 repository functions in place of its inline `text()` statements; keep `session.begin()`/`.commit()`/`.rollback()` calls and all business logic (id parsing, error raising, response shaping) exactly where they are; delete the now-unused module-level SQL constants
- [X] T015 [US1] Update `app/services/inventory.py`'s `emit_crossings` to call `transactions_repo.insert_alerts` (T010) and its `low_stock` function to call `analytics_repo.low_stock_report`/`analytics_repo.now` (T013); delete the now-unused module-level SQL constants — do not move either function to a new file yet, that is Phase 4/5
- [X] T016 [US1] Update `app/services/analytics.py` to call the T012 repository functions in place of its inline `text()` statements; delete the now-unused module-level SQL constants
- [X] T017 [US1] Update `app/background.py`'s `sweep_abandoned` to call `transactions_repo.sweep_abandoned` (T011) in place of its inline `text()` statement
- [X] T018 [US1] Run `uv run pytest` (full suite must pass unchanged) and `grep -rn "text(" app/services app/background.py` (must print nothing) to close out this story

**Checkpoint**: At this point, `app/services/*.py` and `app/background.py` contain zero SQL — all of it lives in `app/db/`. Business logic has not moved yet; that is what US2 and US3 do next.

---

## Phase 4: User Story 2 - Consolidate transaction lifecycle and stock rules into a Transactions layer (Priority: P2)

**Goal**: All transaction-lifecycle business logic — start/scan/complete/get, stock decrement, low-stock alert emission (write side), and the abandonment sweep — lives in `app/transactions/`, calling only `app/db/transactions_repo.py`.

**Independent Test**: `test_transactions_*` contract tests, `test_checkout_flow`, `test_concurrent_completion`, and the completion half of `test_low_stock` pass unchanged; `app/transactions/` contains no `text(` literal and no import from `app.analytics`.

### Implementation for User Story 2

- [ ] T019 [US2] Create `app/transactions/service.py`: move `parse_tx_id`, `public_id`, `start_transaction`, `scan_item`, `_raise_not_open_or_missing`, `complete_transaction`, `get_transaction` from `app/services/transactions.py` verbatim (already SQL-free after US1); update its call site for alert emission to import from `app.transactions.alerts` (T020)
- [ ] T020 [US2] Create `app/transactions/alerts.py`: move `emit_crossings` from `app/services/inventory.py` verbatim (already calling `transactions_repo.insert_alerts` after US1)
- [ ] T021 [US2] Create `app/transactions/sweeper.py`: move `sweep_abandoned` from `app/background.py` verbatim (already calling `transactions_repo.sweep_abandoned` after US1)
- [ ] T022 [US2] Update `app/api/transactions.py` to import from `app.transactions.service` instead of `app.services.transactions` (`svc.start_transaction`, `svc.parse_tx_id`, `svc.scan_item`, `svc.complete_transaction`, `svc.get_transaction`)
- [ ] T023 [US2] Update `app/main.py`'s `_sweep_loop` to call `app.transactions.sweeper.sweep_abandoned` directly instead of `app.background.sweep_abandoned`
- [ ] T024 [US2] Delete `start_transaction`/`scan_item`/`complete_transaction`/`get_transaction`/`parse_tx_id`/`public_id`/`_raise_not_open_or_missing` from `app/services/transactions.py` and `emit_crossings` from `app/services/inventory.py`, and delete `sweep_abandoned` from `app/background.py` — each has now been moved, not copied
- [ ] T025 [US2] Run `test_transactions_*`, `test_checkout_flow`, `test_concurrent_completion`, the completion half of `test_low_stock`, and the full suite; run `grep -rn "text(" app/transactions` and `grep -rn "from app.analytics\|import app.analytics" app/transactions` (both must print nothing)

**Checkpoint**: Transactions layer fully consolidated and independently testable. `app/services/transactions.py` is now empty except imports; `app/services/inventory.py` retains only the low-stock read; `app/background.py` retains only the recompute-scheduling half.

---

## Phase 5: User Story 3 - Consolidate popular-items and low-stock reporting into an Analytics layer (Priority: P3)

**Goal**: All reporting business logic — the popular-items window (compute, recompute-scheduling, read) and the low-stock report read — lives in `app/analytics/`, calling only `app/db/analytics_repo.py`. `app/services/` and `app/background.py` are fully retired.

**Independent Test**: `test_analytics`, `test_popular_items`, and the read half of `test_low_stock` pass unchanged; `app/analytics/` contains no `text(` literal and no import from `app.transactions`; `app/services/` and `app/background.py` no longer exist.

### Implementation for User Story 3

- [ ] T026 [US3] Create `app/analytics/popular_items.py`: move `RANKING_DEPTH`, `_bounds`, `_counts`, `_render`, `recompute`, `read` from `app/services/analytics.py` verbatim (already SQL-free after US1)
- [ ] T027 [US3] Create `app/analytics/low_stock.py`: move the `low_stock` read function from `app/services/inventory.py` verbatim (already calling `analytics_repo.low_stock_report`/`.now` after US1)
- [ ] T028 [US3] Create `app/analytics/scheduler.py`: move `should_recompute` and `recompute_window` from `app/background.py` verbatim
- [ ] T029 [US3] Update `app/api/analytics.py` to import from `app.analytics.popular_items` instead of `app.services.analytics`
- [ ] T030 [US3] Update `app/api/inventory.py` to import from `app.analytics.low_stock` instead of `app.services.inventory`
- [ ] T031 [US3] Update `app/api/transactions.py`'s `scan_item` endpoint and `app/main.py`'s `lifespan` to call `app.analytics.scheduler.should_recompute` / `.recompute_window` directly instead of `app.background.should_recompute` / `.recompute_window`
- [ ] T032 [US3] Delete `app/services/analytics.py`, `app/services/inventory.py`, `app/services/__init__.py`, the now-empty `app/services/` directory, and `app/background.py`
- [ ] T033 [US3] Run `test_analytics`, `test_popular_items`, the read half of `test_low_stock`, and the full suite; run `grep -rn "text(" app/analytics` and `grep -rn "from app.transactions\|import app.transactions" app/analytics` (both must print nothing); confirm `test ! -d app/services && test ! -f app/background.py`

**Checkpoint**: All three user stories complete. Every layer-boundary check in contracts/layer-boundaries.md can now be run against the real, final `app/` tree.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Whole-tree verification that spans all three stories

- [ ] T034 [P] Run all three grep checks from contracts/layer-boundaries.md against the final tree exactly as written there (`app/transactions app/analytics app/api`, both cross-import directions, and `app/db` for stray `commit()`/`rollback()`/`begin(` calls) — all three must print nothing
- [ ] T035 [P] Grep the whole `app/` tree for lingering `app.services` or `app.background` import references (e.g. stale comments in `app/main.py`, `app/errors.py`) and fix any found
- [ ] T036 Re-run the load client at 10 stations / 60 s (`load-client/`) and compare `START_TRANSACTION`/`SCAN_ITEM`/`COMPLETE_TRANSACTION` p50/p95/p99 against the baseline recorded in `specs/001-checkout-backend/plan.md`, confirming SC-004 (no meaningful regression)
- [ ] T037 Run quickstart.md's full validation sequence (steps 1–5) end-to-end and record the result

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 (T001 must exist as a package before anything else touches `app/db/`, though T005–T007 don't strictly need it — kept sequential for a clean checkpoint cadence)
- **User Story 1 (Phase 3)**: Depends on Phase 2 — BLOCKS User Stories 2 and 3 (they consume the repo functions T009–T013 create)
- **User Story 2 (Phase 4)**: Depends on Phase 3 completion (needs SQL-free `services/transactions.py` and `services/inventory.py` to move from)
- **User Story 3 (Phase 5)**: Depends on Phase 3 completion; independent of Phase 4 in principle (US2 and US3 touch disjoint files after US1), but T032's deletion of `app/services/` and `app/background.py` must wait until both US2 (T024) and US3 (T026–T028) have moved everything out — sequenced after Phase 4 above for a simpler single-mover story, not a hard technical requirement
- **Polish (Phase 6)**: Depends on Phases 3, 4, and 5 all being complete

### Parallel Opportunities

- T002, T003 (package inits) in parallel
- T006, T007 (router import updates) in parallel once T005 exists
- T009, T010, T011, T012, T013 (repo module creation) in parallel — five independent new files, no shared state
- T034, T035 in Polish, in parallel

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (Setup) + Phase 2 (Foundational)
2. Complete Phase 3 (User Story 1 — database-access extraction)
3. **STOP and VALIDATE**: full test suite green, zero `text(` outside `app/db/`
4. This alone is a real, shippable improvement: every SQL statement is now named and centralized, even before the business-logic files are physically relocated

### Incremental Delivery

1. Setup + Foundational → package skeleton ready, no router cross-imports
2. User Story 1 → all SQL isolated → validate independently
3. User Story 2 → Transactions layer consolidated → validate independently (US1's work is a prerequisite, already validated)
4. User Story 3 → Analytics layer consolidated, `app/services`/`app/background.py` retired → validate independently
5. Polish → whole-tree boundary check + performance confirmation
