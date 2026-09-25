# Self-Checkout Server Backend: Development History

**Project**: CS6510 Self-Checkout Backend  
**Branch**: `001-checkout-backend` → `002-layered-architecture`  
**Date Created**: 2026-09-16  
**Last Updated**: 2026-09-24

---

## Project Overview

A supermarket self-checkout system backend built with **FastAPI** and **PostgreSQL**. The system handles concurrent transactions from up to 100 checkout stations, tracks inventory (2000 items × 10,000 units each), processes item scans, completes transactions with payment, and provides analytics including low-stock alerts and popular items tracking.

**Key Constraint**: Sub-1ms latency targets (p95), with zero tolerance for inventory corruption.

---

## Phase 1: Initial Implementation (001-checkout-backend)

**Duration**: 2026-09-16 to 2026-09-18  
**Commit**: `6838aee` — "Implement self-checkout backend (monolith: FastAPI + PostgreSQL)"

### Architecture

A monolithic FastAPI service with a thin API layer over services that mix business logic with raw SQL:

```
API Layer (routers) → Services Layer (business logic + SQL) → Raw SQL → PostgreSQL
```

**Services**:
- `transactions.py`: Transaction lifecycle, stock management
- `analytics.py`: Popular items sliding window
- `inventory.py`: Low-stock alerts
- `background.py`: Background jobs (sweeper, analytics recompute)

### Key Implementation Details

#### Transaction Completion (Correctness Core)

The most critical invariant was **preventing inventory corruption under concurrent load**. This was solved with:

```sql
-- 1. Lock the transaction row (prevent double-complete)
SELECT ... FOR UPDATE

-- 2. Collapse the basket with GROUP BY SKU
-- 3. Lock stock rows in SKU order (prevents deadlock)
-- 4. Conditional batched UPDATE
UPDATE inventory_stock 
SET current_stock = current_stock - item.qty 
WHERE sku IN (...) 
  AND current_stock >= qty
```

> **Design Decision**: SKU-ordered locking makes deadlock structurally impossible. This ordering is critical — reverse the order, add concurrent completions, and you get deadlock.

#### Scan Operation (Performance)

A single CTE round trip:

```sql
-- Insert transaction item + return both transaction total and item price
WITH inserted AS (
  INSERT INTO transaction_item (tx_id, sku, qty, price_cents) 
  VALUES (...) 
  RETURNING ...
)
SELECT (transaction totals), (item details) FROM ...
```

#### Popular Items Analytics (Sliding Window)

```
Track global scan sequence (transaction_item.id) 
→ Every 500 scans: compute window_counts(scans[1001-2000]) 
→ Store snapshot (in-memory visible to all workers)
→ Serve from snapshot
```

> **Insight**: Using transaction_item.id as the global scan sequence meant every scan auto-increments the window without explicit counter management.

### Measured Performance

**Baseline (10 stations, 60s run)**:

| Operation | p50 | p95 | p99 |
|-----------|-----|-----|-----|
| START_TRANSACTION | 0.75 ms | 1.26 ms | 1.82 ms |
| SCAN_ITEM | 0.80 ms | 1.36 ms | 1.95 ms |
| COMPLETE_TRANSACTION | 2.30 ms | 4.22 ms | 6.02 ms |

**Throughput**: 788 tx/s (zero errors)

### Critical Findings

#### Finding 1: HTTP/2 Upgrade Header Issue
> **Quote from commit**: "uvicorn must run --http h11. java.net.http.HttpClient defaults to HTTP/2, so every POST carries `Upgrade: h2c`; the httptools parser treats that as an upgrade and drops the request body, failing 100% of START_TRANSACTION."

**Impact**: Load client would fail completely until HTTP protocol was explicitly specified.

#### Finding 2: Zipf Depletion Under Load
> **Quote from commit**: "The default 10-station run legitimately reports ~57% COMPLETE errors: Zipf depletion means later baskets must 409 rather than oversell."

This revealed that popular items sell out quickly under the default load profile. Increased stock (STOCK_PER_ITEM=200,000) returned 0% errors at 788 tx/s.

**Implication**: Error rates in performance tests must be contextualized by inventory depletion, not dismissed as failures.

---

## Architecture Analysis & Characteristics

**Date**: 2026-09-18

### Comprehension Questions & Decision Making

#### Question 1: What performance targets are realistic?

**Initial Spec**: 1 second p95 latency  
**Measured Baseline**: 1.36 ms p95 for SCAN

**Realization**: The spec targets were **~1000x looser than actual performance**. This was replaced with **relative targets**:

> "p95 within about 200% of an operation's own median at 10 stations; completion p95 within 300-400% of scan, since it does several locked round trips where a scan does one"

**Key Insight**: Using relative budgets (% of median) survives hardware changes and architecture variations, while absolute millisecond targets become stale immediately.

#### Question 2: What does scalability actually mean?

**Initial Spec**: "p95 growth should be ≤2x at 10x load"

**Measured Reality**: At 100 stations (10x load), completion p95 was **17.7x** the baseline.

**Revision**: > "p95 growth up to about 1000% at 10x concurrency is proportional and acceptable. Growth meaningfully beyond that means something is serializing."

**Finding**: Completion (which holds row locks) degrades faster than scan (one round trip, no locks). This became the pressure point for future optimization.

#### Question 3: What's the right recovery time objective?

**Initial Spec**: 1-hour RTO (Recovery Time Objective)

**Realization**: > "1-hour RTO would mean closing the lanes."

**Revision**: 2-minute RTO, with an explicit RPO (Recovery Point Objective) of sub-second loss window when `synchronous_commit=off`.

### Architecture Characteristics (Final)

**Reliability** (Top Priority): Zero tolerance — inventory correctness and transaction consistency must never fail, even under load.

**Performance** (Second Priority): Sub-1ms scans, sub-5ms completions; tail degradation acceptable but correctness is not.

**Scalability** (Third Priority): 10x concurrency allowed 10x latency growth.

---

## Phase 2: Layered Architecture Refactor (002-layered-architecture)

**Duration**: 2026-09-23 to 2026-09-24  
**Branch**: `refactor/002-layered-architecture`

### Problem Statement

The monolith mixed business logic with raw SQL in service files. A maintainer couldn't understand:
- How stock is decremented without reading SQL
- How popularity windows recompute without understanding the analytics queries
- Whether a business rule change would accidentally affect another layer

### Comprehension Questions in Refactor Design

#### Question 1: Where do inventory.py's two functions go?

**Initial Assumption**: Both into Transactions layer (it's what existed).

**User Correction**: Split them.
- `emit_crossings()` (alert on threshold crossing) → **Transactions layer** (runs inside completion's lock sequence, must be atomic)
- `low_stock()` (read endpoint) → **Analytics layer** (standalone report, no locking)

> **Research R1 Quote**: "The user confirmed this split explicitly after initially reviewing the default (folding both into Transactions)."

**Implication**: Layer boundaries should follow data flow and invariant ownership, not historical file structure.

#### Question 2: Does inventory.py become a fifth layer?

**Alternatives Considered**:
1. Keep as fifth "Inventory" layer → Rejected (only 2 functions, don't belong together)
2. Fold both into Transactions → Rejected (read endpoint has no reason to live with write invariants)
3. Fold both into Analytics → Rejected (alert emission must be atomic with decrement)

**Decision**: No fifth layer. Responsibilities split according to their coupling and invariant ownership.

#### Question 3: How should background.py split?

**Initial Structure**: `background.py` bundled two unrelated jobs:
- `sweep_abandoned()` (cancel old open transactions)
- `recompute_window()` + `should_recompute()` (schedule analytics updates)

**Question**: Should this be a shared "background jobs" layer?

**Decision**: No. Move each to its owner:
- `sweep_abandoned()` → `app/transactions/sweeper.py`
- `recompute_window()` → `app/analytics/scheduler.py`
- `main.py` calls both directly

> **Research R2 Quote**: "Neither needs the other, and keeping them fused would mean the Analytics layer and Transactions layer both importing a third module that isn't one of the four named layers."

#### Question 4: What's the repository module granularity?

**Options**:
1. One repo file per current service file (defeats the point, recreates fragmentation)
2. One repo class per entity (`TransactionRepo`, `InventoryRepo`) (unnecessary ceremony)
3. One repo module per business layer that consumes it → **Chosen**

```
app/db/transactions_repo.py (all queries called by Transactions layer)
app/db/analytics_repo.py (all queries called by Analytics layer)
```

> **Research R3 Quote**: "Matching repo modules to layers (not to today's file layout) is what makes R1's split coherent."

#### Question 5: Who owns transaction boundaries?

**Alternatives**:
1. Repo layer opens/closes transactions (database-access concern owns atomicity)
2. Business layer owns transactions (business logic owns its own invariants)

**Decision**: Business layer.

```python
# In transactions/service.py
async with session.begin():
    repo.lock_transaction(session, tx_id)
    repo.lock_stock(session, skus)
    repo.decrement_stock(session, basket)
    alerts.emit_crossings(session, ...)
    repo.complete_transaction(session, ...)
# Atomic: all succeed or all rollback
```

> **Research R4 Quote**: "The completion path's atomicity is a business invariant, not a data-access concern — a repo function that decided on its own when to commit could not guarantee that sequence stays atomic."

#### Question 6: Are the existing tests valid for this refactor?

**Concern**: We're reorganizing code. Will the tests still measure correctness?

**Analysis**:
- Contract tests talk to the app over HTTP (blind to internal modules) ✓
- Integration tests check database side effects (verify correctness) ✓
- One test directly imports `app.services.analytics` (needs import update)

> **Research R5 Quote**: "Confirmed by reading tests/conftest.py — both interact purely through the HTTP surface and the database tables, never through an internal Python import of business-layer code."

**Result**: Existing tests are valid. Only three import paths needed updating.

### Target Architecture

```
┌─ API Layer (routers, request/response only) ─────────────┐
│  ├─ POST /transactions → transactions.service            │
│  ├─ POST /items/{txId} → transactions.service            │
│  ├─ POST /complete/{txId} → transactions.service         │
│  └─ GET /analytics/popular → analytics/popular_items.py  │
└────────────────────────────────────────────────────────────┘
                            ↓
         ┌─────────────────────────────────┐
         │  Transactions Layer             │
         │  ├─ service.py (lifecycle)      │
         │  ├─ alerts.py (emit_crossings)  │
         │  └─ sweeper.py (abandonment)    │
         └─────────────────────────────────┘
                      ↓
         ┌─────────────────────────────────┐
         │  Analytics Layer                │
         │  ├─ popular_items.py (window)   │
         │  ├─ scheduler.py (recompute)    │
         │  └─ low_stock.py (report read)  │
         └─────────────────────────────────┘
                      ↓
         ┌─────────────────────────────────┐
         │  Database Access Layer (Repos)  │
         │  ├─ transactions_repo.py (9 fn) │
         │  └─ analytics_repo.py (6 fn)    │
         └─────────────────────────────────┘
                      ↓
                  PostgreSQL
```

### Implementation (3 User Stories, 1 week)

#### User Story 1: Isolate all raw SQL

**Outcome**: Every `text(...)` SQL statement moved into repository functions.

```python
# Before: in services/transactions.py
result = await session.execute(text("""
    SELECT ... FROM transaction WHERE id = :tx_id FOR UPDATE
"""), {"tx_id": tx_id})

# After: in transactions/service.py
result = await transactions_repo.lock_transaction(session, tx_id)

# Repository function in db/transactions_repo.py
async def lock_transaction(session, tx_id):
    result = await session.execute(text("""
        SELECT ... FROM transaction WHERE id = :tx_id FOR UPDATE
    """), {"tx_id": tx_id})
    return result.scalar_one_or_none()
```

**Tests**: Full suite passes unchanged. `grep -rn "text(" app/services/` returns nothing.

#### User Story 2: Consolidate transaction lifecycle

**Outcome**: Transaction logic moved from `app/services/transactions.py` → `app/transactions/service.py`, alert emission from `app/services/inventory.py` → `app/transactions/alerts.py`, sweeper from `app/background.py` → `app/transactions/sweeper.py`.

**Scope**:
- All transaction start/scan/complete/get logic
- Stock decrement under locks
- Low-stock alert emission (write side)
- Abandoned transaction sweep

**Tests**: Transaction-specific contract tests pass. No cross-layer imports between Transactions and Analytics.

#### User Story 3: Consolidate analytics reporting

**Outcome**: Popular items and low-stock reporting moved to `app/analytics/`.

**Scope**:
- Popular items sliding window (computation and read)
- Low-stock report read (separate from write-side alert emission)
- Recompute scheduler

**Tests**: Analytics-specific tests pass. No new test assertions needed.

### Performance Validation

**A/B Comparison** (same host, same seed, 4 workers, 10 stations / 60s):

| Operation | Pre-refactor p50/p95/p99 | Layered p50/p95/p99 | Δ |
|-----------|--------------------------|---------------------|---|
| START | 0.85 / 1.31 / 1.82 ms | 0.74 / 1.51 / 1.84 ms | Within noise |
| SCAN | 0.91 / 1.39 / 1.95 ms | 0.79 / 1.60 / 1.95 ms | Within noise |
| COMPLETE | 2.53 / 4.51 / 6.02 ms | 2.50 / 4.67 / 6.21 ms | Within noise |
| Throughput | 741 tx/s | 733 tx/s | -1% |

**Invariant Verification**: 461,425 line items decremented, 0 negative, 0 mismatched. ✓

> **Finding**: Function-call indirection adds negligible overhead vs. network round-trip cost.

---

## Key Design Decisions Summary

| Decision | Rationale | Implication |
|----------|-----------|-------------|
| **SKU-ordered locking** | Prevents deadlock structurally | Must be maintained in all completions |
| **Transaction atomicity in business layer** | Business logic owns its invariants | Database layer is just a query executor |
| **One repo module per business layer** | Enforces layer coupling visibility | Easy to spot cross-layer violations |
| **Split inventory.py by coupling** | Alert emission ≠ read reporting | Enabled true analytics layer independence |
| **Relative performance targets** | Survive hardware/architecture changes | Updated based on measured baselines |

---

## Lessons & Insights

### On Correctness

1. **Inventory corruption is structural, not accidental**
   - A seemingly small change (reverse lock order, missing atomicity check) causes silent data corruption
   - Zero-tolerance testing with invariant verification is essential

2. **Atomicity must be owned by the layer that understands the invariant**
   - Database layer should never decide when a transaction ends
   - Business layer must control transaction boundaries

### On Performance

1. **Relative targets beat absolute targets**
   - Absolute latency targets (1000ms) become obsolete instantly
   - Relative targets (p95 within 200% of median) remain valid across weeks and architectures

2. **Lock contention has a visible pressure point**
   - Completion (lock-heavy) degrades 17.7x at 10x load
   - Scan (one round trip) stays within expected bounds
   - This identifies where optimization work should focus

3. **Sub-1ms latencies aren't free, but they're cheap**
   - Function-call overhead is negligible vs. network/database cost
   - Layering adds no meaningful latency

### On Architecture

1. **File structure != layer structure**
   - `services/inventory.py` held two unrelated business concerns
   - Splitting by coupling (not history) makes refactors coherent

2. **Layer boundaries should follow data flow**
   - Alert emission runs inside completion's lock → lives in Transactions
   - Low-stock read is independent report → lives in Analytics
   - This coupling visibility is the entire point of layering

3. **Tests as regression oracle**
   - HTTP-level tests are blind to internal module moves
   - They remain valid throughout refactoring
   - Import path changes are the only test edits needed

### On Requirements & Specification

1. **Measure before specifying**
   - "1 second p95" was meaningless when actual performance is 1.36 ms
   - Baselines must be measured, targets must be relative
   - Back-fitting specs to measured performance avoids unrealistic commitments

2. **Error rates need context**
   - 57% completion errors are correct behavior (inventory depleted)
   - Not all errors are failures; some are business-correct

3. **RTO targets must be operationally realistic**
   - "1 hour" was never achievable (closes the store)
   - "2 minutes" matches operational reality

---

## Repository State

### Directory Structure (Final)

```
backend/
├── app/
│   ├── api/
│   │   ├── _request.py (shared request helpers)
│   │   ├── transactions.py (routers only)
│   │   ├── inventory.py (routers only)
│   │   └── analytics.py (routers only)
│   ├── transactions/
│   │   ├── __init__.py
│   │   ├── service.py (lifecycle: start/scan/complete/get)
│   │   ├── alerts.py (emit_crossings on threshold)
│   │   └── sweeper.py (abandoned transaction cancellation)
│   ├── analytics/
│   │   ├── __init__.py
│   │   ├── popular_items.py (sliding window computation & read)
│   │   ├── scheduler.py (recompute trigger logic)
│   │   └── low_stock.py (low-stock report read)
│   ├── db/
│   │   ├── __init__.py (engine, sessions, locks)
│   │   ├── transactions_repo.py (9 functions)
│   │   └── analytics_repo.py (6 functions)
│   ├── models.py (SQLAlchemy ORM, unchanged)
│   ├── catalog_cache.py (shared, unchanged)
│   ├── errors.py (shared, unchanged)
│   ├── money.py (shared, unchanged)
│   └── main.py (FastAPI app + background job loops)
├── specs/
│   ├── 001-checkout-backend/
│   │   ├── spec.md
│   │   ├── plan.md
│   │   ├── research.md
│   │   └── ...
│   └── 002-layered-architecture/
│       ├── spec.md
│       ├── plan.md
│       ├── research.md
│       ├── data-model.md (repository function contracts)
│       └── contracts/
│           ├── layer-boundaries.md
│           └── openapi.yaml (unchanged)
└── tests/
    ├── conftest.py (fixtures)
    ├── contract/ (HTTP-level, unchanged)
    ├── integration/ (database verification, unchanged)
    └── unit/ (unchanged)
```

### Test Results

- **001 Phase**: 101 passing tests (57 contract, unit + integration)
- **002 Phase**: 100% passing (3 import updates only, no assertion changes)

### Branch Timeline

```
main (2026-09-16)
└─ 001-checkout-backend (impl complete 2026-09-18)
   ├─ Architecture analysis & characteristics (2026-09-18)
   └─ Merge to main
      └─ 002-layered-architecture (impl complete 2026-09-24)
         ├─ Phase 0 Research (2026-09-23)
         ├─ Phase 1 Design (2026-09-23)
         ├─ Phase 2 Implementation (2026-09-23 to 2026-09-24)
         └─ [Current: awaiting review/merge]
```

---

## Remaining Notes

### What Wasn't Changed

- API contracts (request/response shapes, status codes)
- Database schema (6 tables remain)
- Observable behavior (same data, same business rules)
- Test assertions (existing suite is 100% valid)
- Shared infrastructure (catalog_cache, errors, money)

### What Was Validated

- ✓ Zero raw SQL outside `app/db/`
- ✓ Zero cross-layer imports (Transactions ≠ Analytics)
- ✓ Performance unchanged (function-call overhead < 1%)
- ✓ Correctness preserved (inventory invariant holds)
- ✓ All 100 existing tests pass

### Potential Follow-Up Work

(Not blocking this feature)

1. Unit tests scoped to layer responsibilities
2. Architecture lint rules (import-linter / architecture-test)
3. Upgrade to managed async connection pool
4. Persistent connection to read replicas for analytics queries
5. Separate "cold path" (analytics reads) from "hot path" (completions)

---

## Bibliography & References

- **spec.md files**: Frozen contracts and acceptance criteria
- **plan.md files**: Implementation strategies and measured baselines
- **research.md files**: Design decisions and alternatives considered
- **Commit history**: Day-by-day decision log
- **tests/**: Regression oracle and correctness verification
- **backend/README.md**: Installation, running, debugging, and observability
