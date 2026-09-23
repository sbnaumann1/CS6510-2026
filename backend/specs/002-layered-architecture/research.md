# Phase 0 Research: Layered Architecture Refactor

**Feature**: `002-layered-architecture` | **Date**: 2026-09-23

There are no external-technology unknowns here — no new dependency, no new storage engine, no new
protocol. Every open question is a module-boundary decision about the existing codebase. Each is
resolved below in the research.md format for consistency with 001, even though "alternatives" here
means "alternative ways to cut the same code," not "alternative libraries."

## R1. Where do `services/inventory.py`'s two responsibilities go?

**Decision**: Split them. `emit_crossings` (called from inside `complete_transaction`'s locked
section) moves to `app/transactions/alerts.py`. `low_stock()` (the standalone `GET
/inventory/low-stock` read) moves to `app/analytics/low_stock.py`.

**Rationale**: The four layers the user specified are API / Transactions / Analytics / Database
access — there is no fifth "inventory" layer, so this file's two functions have to land somewhere
in the other four, and they don't have the same shape. `emit_crossings` only ever runs as a side
effect inside the completion transaction's lock sequence (`services/transactions.py`
`complete_transaction`, step 5) — moving it anywhere but the Transactions layer would mean that
layer reaching across a layer boundary mid-transaction to fire a business rule, which is exactly
the kind of coupling this refactor is meant to remove. `low_stock()` has no such coupling: it is a
standalone read with its own lock-free query, same shape as `analytics.read()` for popular items
(a report over state some other flow produced). The user confirmed this split explicitly after
initially reviewing the default (folding both into Transactions).

**Alternatives considered**: (a) Fold both into Transactions (the original default) — rejected by
the user because a read-only report has no natural reason to live next to write-path invariants
just because they share a source file today. (b) A fifth "Inventory" layer — rejected, not one of
the four layers specified, and would only ever hold two functions that don't otherwise depend on
each other. (c) Fold both into Analytics — rejected because alert emission's atomicity requirement
(same transaction as the decrement, SC-005 in the 001 spec) makes it Transactions' concern, not a
report.

## R2. Where does `background.py` split?

**Decision**: `should_recompute` + `recompute_window` (the analytics-window scheduling half) move
to `app/analytics/scheduler.py`. `sweep_abandoned` (the transaction-abandonment half) moves to
`app/transactions/sweeper.py`. `main.py`'s `_sweep_loop` calls both directly instead of importing
one `background` module.

**Rationale**: The file today bundles two unrelated background jobs only because they're both
"stuff that isn't request/response." One is scheduling logic for the Analytics layer's own
recompute; the other cancels stale Transactions-layer state. Neither needs the other, and keeping
them fused would mean the Analytics layer and Transactions layer both importing a third module
that isn't one of the four named layers.

**Alternatives considered**: Keep `background.py` as a thin dispatcher that imports from both new
locations — rejected as an unnecessary indirection once `main.py` can call two functions directly;
nothing else imports `background.py`.

## R3. Does `app/db.py` become a package, and what's the repo-module granularity?

**Decision**: `app/db.py` becomes `app/db/__init__.py`, keeping `engine`, `SessionLocal`,
`get_session`, `advisory_lock`, and `dispose_engine` importable from `app.db` exactly as today, so
`main.py`'s and every business module's existing `from app.db import ...` lines don't change. Two
sibling repo modules are added: `app/db/transactions_repo.py` and `app/db/analytics_repo.py` — one
per business layer that consumes it, not one per current service file.

**Rationale**: Matching repo modules to layers (not to today's file layout) is what makes R1's
split coherent: the low-stock `SELECT` and the popular-items queries both live in
`analytics_repo.py` even though they came from two different files, because both are called only
from the Analytics layer. A repo module per originating file would recreate the same fragmentation
the layering is meant to fix.

**Alternatives considered**: One flat `app/db/repo.py` for everything — rejected, defeats the
point of grouping data access by the layer that owns it, and would make R6's "no cross-layer
internal import" rule unenforceable by inspection. A repo class per entity (`TransactionRepo`,
`InventoryRepo`, ...) — rejected as unneeded ceremony; these are stateless functions over a
passed-in session, not objects with their own state (YAGNI, per the Constitution Check's
Simplicity gate).

## R4. Who owns transaction-boundary control (`session.begin()` / commit / rollback)?

**Decision**: The business layer (Transactions or Analytics), never the database-access layer.
`complete_transaction`'s `async with session.begin(): ...` block stays in
`app/transactions/service.py`, wrapping calls to `app/db/transactions_repo.py` functions that
themselves never call `commit`/`rollback`/`begin`.

**Rationale**: The completion path's atomicity (lock transaction → lock stock → conditional
decrement → emit alerts → finalize, all-or-nothing) is a business invariant, not a data-access
concern — a repo function that decided on its own when to commit could not guarantee that
sequence stays atomic. This matches spec FR-005 exactly and was already the plan surfaced in the
original architecture analysis; nothing here changed with R1's revision.

**Alternatives considered**: A unit-of-work object passed down through the repo layer — rejected
as more machinery than four small repo modules need; the existing pattern (session passed as a
plain argument, business layer opens/closes the transaction) already works and this feature does
not need to change it.

## R5. Are the existing tests a valid regression oracle for this refactor?

**Decision**: Yes, unmodified. `tests/conftest.py`'s `client` fixture builds the real
`create_app()` behind an `httpx.AsyncClient` and talks to it over ASGI/HTTP; contract tests assert
on status codes and response bodies, integration tests assert on database side effects via a raw
`session` fixture. Neither references `app.services`, `app.background`, or any other internal
module path.

**Rationale**: Confirmed by reading `tests/conftest.py` and one integration test
(`test_low_stock.py`) during specification — both interact purely through the HTTP surface and the
database tables, never through an internal Python import of business-layer code. This is exactly
what spec FR-009 requires and what SC-001 measures.

**Alternatives considered**: N/A — this is a factual check, not a design choice.

## R6. How is "no cross-layer internal import" (spec FR-006) enforced?

**Decision**: By code review / grep at implementation time (`grep -rn "from app.transactions" app/analytics/` and the reverse), not by a new lint rule or CI gate — this feature does not add tooling.

**Rationale**: Spec FR-006 is a design constraint the four-layer split is supposed to make visible
by construction: once `emit_crossings` isn't reachable from `app/analytics/*` and `low_stock()`
isn't reachable from `app/transactions/*`, the rule holds because there's nothing left in either
package that would violate it. Adding an enforcement tool is out of scope for a structural
refactor whose own success criteria (SC-002, SC-003) are already stated as "verified by
inspection."

**Alternatives considered**: An import-linter / architecture-test (e.g. `import-linter` contracts)
— reasonable future hardening, noted as a non-blocking follow-up, not required by any FR or SC
here.
