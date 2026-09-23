# Feature Specification: Layered Architecture Refactor

**Feature Branch**: `002-layered-architecture`

**Created**: 2026-09-23

**Status**: Draft

**Input**: User description: "Refactor the self-checkout backend (currently a monolith with a thin API layer over services that mix business logic with raw SQL) into a layered architecture with four explicit layers: (1) API layer — FastAPI routers; request parsing/validation and response shaping only, no SQL, no business rules. (2) Transactions layer — owns transaction lifecycle (start/scan/complete/get), the completion invariants (deterministic lock ordering, conditional stock decrement, atomicity of decrement+alert+status-change), low-stock alert emission on crossing, and the abandoned-transaction sweep. (3) Analytics layer — owns the popular-items hopping-window computation and read path, and the recompute-scheduling half of background.py. (4) Database access layer — every raw SQL statement extracted into repository-style functions that take a session + params and return rows/scalars only, no business rules, no transaction-boundary control. Non-goals: no change to API contracts, request/response shapes, the error catalog, or database schema. The existing contract/integration test suite must keep passing unchanged."

## User Scenarios & Testing *(mandatory)*

<!--
  This feature has no end-user-facing behavior change; the "users" of this
  restructuring are the engineers who build and maintain the checkout backend.
  Each story is an independently-shippable slice of the layering, ordered so
  that later stories can build on earlier ones without the earlier stories
  depending on the later ones.
-->

### User Story 1 - Isolate all raw SQL behind a database-access layer (Priority: P1)

A maintainer working on business logic (transaction rules or analytics rules) should never need to read or write a raw SQL string to understand or change what a business rule does. Every SQL statement currently embedded inside `services/transactions.py`, `services/analytics.py`, and `services/inventory.py` is extracted into repository-style functions, grouped by the domain they serve, that do nothing but accept a session and parameters and return rows or scalars.

**Why this priority**: This is the foundation every other layer depends on. Until raw SQL is out of the business-logic files, "transactions layer" and "analytics layer" are just names for the same fused files that exist today — none of the other stories can be verified as a real separation until this one lands.

**Independent Test**: Can be fully tested by running the existing contract and integration test suite (which drives the app over HTTP and is blind to internal module boundaries) and confirming it passes unchanged, while grepping the transactions/analytics business modules and finding zero `text(...)`/raw-SQL literals outside the new database-access modules.

**Acceptance Scenarios**:

1. **Given** the extracted database-access layer, **When** a business-layer function needs data, **Then** it calls a named repository function instead of constructing or executing a SQL statement directly.
2. **Given** the full contract/integration test suite, **When** it is run against the refactored code, **Then** every test that passed before the refactor still passes, with no test file changes required.

---

### User Story 2 - Consolidate transaction lifecycle and stock rules into a Transactions layer (Priority: P2)

A maintainer changing checkout behavior (starting a transaction, scanning an item, completing a basket, reading transaction state, decrementing stock, emitting low-stock alerts, or sweeping abandoned transactions) should find all of that logic in one cohesive layer, calling down into the database-access layer from Story 1 rather than embedding SQL itself.

**Why this priority**: This is the layer with the most business-critical invariants (deadlock-free lock ordering, atomic stock decrement, exactly-once alert emission) — consolidating it right after the data-access extraction, while that extraction is fresh, minimizes the risk of silently changing these invariants.

**Independent Test**: Can be fully tested by running the transaction-related contract tests (`test_transactions_*`) and the concurrency-sensitive integration tests (`test_checkout_flow`, `test_concurrent_completion`, `test_low_stock`) unchanged, and confirming the transactions layer's modules contain no references to the analytics layer's internals.

**Acceptance Scenarios**:

1. **Given** a completed checkout, **When** stock for a scanned SKU crosses the low-stock threshold, **Then** an alert is recorded exactly once, atomically with the stock decrement, exactly as it is today.
2. **Given** two concurrent completions that both need locks on overlapping SKUs, **When** both run, **Then** the deterministic lock ordering still prevents deadlock and stock never goes negative.
3. **Given** an OPEN transaction older than the configured abandonment window, **When** the sweep runs, **Then** it is cancelled exactly as it is today, with no stock changes.

---

### User Story 3 - Consolidate popular-items analytics into an Analytics layer (Priority: P3)

A maintainer changing how the popular-items window is computed, recomputed, or read should find all of that logic in one cohesive layer, independent of transaction-completion internals, calling down into the database-access layer from Story 1.

**Why this priority**: Lower risk and lower coupling than Story 2 — analytics only reads the scan sequence produced by transactions, so it can be separated last without blocking the higher-risk work above.

**Independent Test**: Can be fully tested by running the analytics contract and integration tests (`test_analytics`, `test_popular_items`) unchanged, and confirming the recompute-scheduling logic (currently split across `background.py`) is reachable from a single analytics-layer entry point rather than being interleaved with the transaction-sweep logic.

**Acceptance Scenarios**:

1. **Given** enough scans to cross a slide-interval boundary, **When** the next scan happens, **Then** the popular-items window recomputes exactly as it does today and is served identically on the next read.
2. **Given** no snapshot yet computed, **When** `GET /analytics/popular-items` is called, **Then** it falls back to computing the window on demand exactly as it does today.

---

### Edge Cases

- What happens when a business-layer function needs several repository calls inside one atomic unit (e.g., transaction completion's lock-then-decrement-then-alert sequence)? The business layer, not the database-access layer, must retain control of the session/transaction boundary so this sequence stays atomic.
- What happens to code that today is shared by more than one business concern (the in-memory catalog cache, the `ApiError`/error-catalog module, money formatting)? These remain shared infrastructure usable by any layer rather than being force-fit into one of the four layers.
- How is a regression distinguished from an intended behavior change during this refactor? Any test failure after the refactor is treated as a regression to be fixed, not a spec to be updated, since this feature is explicitly structural-only (see Non-Goals below).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The API layer MUST contain no raw SQL and MUST NOT begin, commit, or roll back a database transaction; it MUST delegate all business logic to the transactions layer or the analytics layer.
- **FR-002**: The database-access layer MUST expose only functions that accept a session and parameters and return rows or scalars; it MUST NOT contain business validation, error-raising, or transaction-boundary control.
- **FR-003**: The transactions layer MUST own all business logic for starting, scanning, completing, and reading transactions, including deterministic stock-lock ordering, the atomic conditional stock decrement, low-stock alert emission on threshold crossing, and the abandoned-transaction sweep.
- **FR-004**: The analytics layer MUST own all business logic for the popular-items window, including recompute scheduling/execution and the read path, independent of the transactions layer's internal modules.
- **FR-005**: Transaction-boundary control (begin/commit/rollback) MUST be owned by whichever business layer (transactions or analytics) understands the invariant being protected, never by the database-access layer.
- **FR-006**: The transactions layer and the analytics layer MUST NOT import each other's internal modules directly; any interaction between them MUST go through the database-access layer or explicitly shared infrastructure.
- **FR-007**: The refactor MUST NOT change any API request/response shape, error code, or HTTP status code documented in the existing contracts.
- **FR-008**: The refactor MUST NOT change the database schema.
- **FR-009**: The existing contract and integration test suites MUST pass unchanged (no test file edits) after the refactor is complete.

### Key Entities

- **API Layer**: The set of FastAPI routers and request/response handling code; the only layer that speaks HTTP.
- **Transactions Layer**: Owns transaction lifecycle, stock decrement, low-stock alerting, and transaction abandonment — the business rules governing an individual checkout.
- **Analytics Layer**: Owns the popular-items hopping-window computation, recompute scheduling, and read path — the business rules governing aggregate reporting.
- **Database Access Layer**: The set of repository-style functions that are the only code in the system permitted to contain SQL.
- **Shared Infrastructure**: Cross-cutting code usable by any layer without belonging to one — the in-memory catalog cache, the error-catalog contract, and money formatting.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the existing contract and integration tests pass without any modification to the test files after the refactor.
- **SC-002**: Zero raw SQL statements exist outside the database-access layer's modules, verified by inspection.
- **SC-003**: A maintainer can identify every module responsible for a given business rule (e.g., "how is low stock detected," "how is the popular-items window recomputed") within a single layer's modules, without reading any SQL text.
- **SC-004**: Observed request latency for the hot paths (scan, complete) after the refactor stays within the ranges already measured and documented for the current monolith, confirming the added layering introduces no meaningful overhead.

## Assumptions

- The current `services/inventory.py` responsibilities (low-stock read, alert-crossing emission) fold into the Transactions layer rather than becoming a fifth layer, since alert emission is invoked from inside the transaction-completion invariant and the low-stock read has no independent locking behavior of its own.
- The in-memory catalog cache (`catalog_cache.py`) remains shared infrastructure outside the four layers rather than part of the database-access layer, since it is a process-lifetime cache rather than a per-request query.
- `errors.py` (the `ApiError` contract) and `money.py` remain a shared kernel importable by every layer.
- The existing contract/integration test suite, which drives the app over HTTP, is the authoritative regression check for this refactor; no new test infrastructure is required to consider this feature complete, though new layer-scoped unit tests are a reasonable follow-on and not blocking.
- This is a structural-only refactor: no API contract, schema, or observable behavior is intended to change, so any observed behavior difference after implementation is a bug to fix, not a spec update.
