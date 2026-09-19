## Architecture Characteristics: Requirements

### Brainstorm

**Availability**: Checkout is the revenue path — when it is down, the store stops taking money. Target 99.9% during store hours (about 45 seconds of downtime per twelve-hour day), with the qualification that a single checkout station failing must never affect the others. *Week 1 does not meet this.* The monolith is one process against one PostgreSQL instance, so either one is a single point of failure. Redundancy is what the later weeks should be judged on.

**Continuity**: After a crash or restart, catalog, stock, transactions, and analytics must come back consistent with no manual repair. Realistic recovery objectives for a checkout system:
- **RTO ≤ 2 minutes.** An hour of downtime means closing the lanes; the earlier "1 hour maximum" was not a serious target for this domain.
- **RPO ≤ 1 second.** We run PostgreSQL with `synchronous_commit = off`, which trades an fsync per commit for throughput. Commits survive a process crash, but an OS or hardware failure can lose up to roughly 600 ms of them (PostgreSQL bounds the window at three times the 200 ms WAL writer delay). That is a deliberate, stated tradeoff — not an accident — and it fits inside a 1-second RPO with little room to spare.

**Performance**: Latency has to be invisible to the customer, not merely "fast". Grounding the budget in the checkout experience rather than in what we happened to measure:
- **Scan ≤ 50 ms p95.** Scanning is the repeated action — a 20-item basket pays this cost 20 times. Under ~100 ms reads as instantaneous, so 50 ms leaves room for the scanner hardware and network on top of the API.
- **Complete ≤ 200 ms p95.** Paying happens once per basket, and a brief pause at the payment moment is socially normal.
- **No request over 1 s, ever.** Past a second the customer looks for staff, which costs more than the transaction.

Completion is allowed a larger budget than scanning on purpose: it is five database round trips under row locks (lock the transaction, collapse the basket, lock stock in SKU order, decrement conditionally, finalize) against one for a scan. Treating all three operations as one number would hide that.

**Recoverability**: A mid-run crash must restore persisted inventory and analytics without manual intervention. Open transactions are deliberately *not* recovered: they hold no stock (inventory moves only at completion), and a sweeper cancels them after 5 minutes. Resuming a customer's in-progress basket across a restart is a future feature, not a week-1 requirement.

**Reliability**: The hard invariant. For every SKU, `initial stock − final stock` must equal the number of units sold in completed transactions, stock must never go negative, and it must never be *silently clamped* to zero. Concurrent completions must preserve this. No customer may be charged for a basket that was only partially in stock — completion is all-or-nothing. This is the one requirement with no acceptable tolerance: a system that fails it is wrong no matter how fast it is.

**Robustness**: Invalid transaction IDs, unknown SKUs, empty baskets, and malformed bodies must produce the contract's documented error for each case without corrupting state or killing the service. When two transactions contend for the same SKU, the outcome must be deterministic rather than racy: one completes, the other receives a clear `409 INSUFFICIENT_STOCK`, and neither deadlocks nor oversells. Contention is resolved, not retried blindly.

**Scalability**: Going from 10 to 100 stations is 10x the concurrency, so some degradation is expected and fine — what matters is that it stays *sub-linear* and that correctness is unaffected:
- Per-operation p95 at 100 stations should grow no more than ~10x its 10-station value (like-for-like, p95 against p95 — the earlier "p95 within 2x of the baseline *average*" compared different statistics and was unachievable for any architecture at 10x load).
- Zero dropped requests, zero timeouts, zero 5xx.
- The reliability invariant must hold identically at 100 stations. Throughput may fall; correctness may not.

**Configurability**: Low-stock threshold, popular-item window size, slide interval, stock levels, worker count, and database connection must all be settable via environment variables without touching code.

**Extensibility**: New business rules, inventory attributes, analytics outputs, or endpoints should be addable without rewriting the transaction or inventory core.

**Installability**: A clean machine should reach a running, seeded system in a small number of documented commands, and resetting to a known-good state must be a single reliable command — every measured run depends on starting from identical state.

**Maintainability**: The error-prone areas — transaction lifecycle, inventory decrement under concurrency, and sliding-window analytics — belong in separate modules with clear boundaries (later in the semester, separate services), so a change in one does not force a rewrite elsewhere. A reader should be able to determine how stock is decremented, how double-completion is prevented, and how popularity is recomputed without reading the whole codebase.

**Upgradeability**: Schema and logic changes should ship through migrations rather than manual database repair. Currently the ORM models are the schema source of truth and `seed.py` recreates from scratch, which is adequate for a benchmark that resets before every run but would not survive real data. Alembic is the intended next step.

**Security**: Customers must never see each other's purchase information, enforced at the API level. *Not implemented in week 1, and worth stating plainly rather than aspirationally:* the contract defines no authentication, the service runs plaintext HTTP on localhost, and nothing is encrypted at rest. For a benchmark harness on a single machine that is acceptable; for anything real, transport security, authentication, and secret management are prerequisites, not enhancements.

**Supportability**: Structured logs and stable machine-readable error codes for transaction failures, stock conflicts, and analytics problems, so failures under load can be diagnosed from the logs alone. Critically, a *legitimate* error (stock ran out) must be distinguishable from a *fault* (the service broke) — otherwise a report full of 409s is unreadable.

**Usability/achievability**: Easy to start, reset, and debug locally, so a change can be validated in minutes rather than as a long setup ritual.

### Measured Baseline

Recorded so the requirements above can be checked rather than asserted. Apple Silicon, 11 cores / 18 GB; PostgreSQL 17 over a Unix socket; 4 uvicorn workers. Full reports in `load-client/reports/`.

Normal run — 10 stations / 60 s, stocked so nothing depletes, 0.00% errors:

| Operation | p50 | p95 | p99 | Budget (p95) |
| --- | --- | --- | --- | --- |
| `START_TRANSACTION` | 0.75 ms | 1.26 ms | 1.94 ms | ≤ 50 ms |
| `SCAN_ITEM` | 0.80 ms | 1.36 ms | 2.06 ms | ≤ 50 ms |
| `COMPLETE_TRANSACTION` | 2.30 ms | 4.22 ms | 5.79 ms | ≤ 200 ms |

788 transactions/s, 8 239 items/s.

Stress — 100 stations / 120 s: `START` 6.14 / 12.95 / 20.74 ms, `SCAN` 6.24 / 12.98 / 20.55 ms, `COMPLETE` 9.89 / 74.60 / 189.53 ms; 9 296 items/s; no timeouts, no 5xx; invariant verified on 225 431 units.

Degradation at 10x concurrency (p95 against p95): `SCAN` 9.6x, `START` 10.3x, `COMPLETE` **17.7x**. Scanning scales sub-linearly as required and starting sits essentially on the linear boundary; **completion is the clear miss** at nearly twice linear. The cause is lock contention on the hot SKUs — the Zipf workload drives a handful of SKUs into most baskets, so completions serialize behind the same row locks. That is precisely the pressure point the later distributed weeks should be compared on, and it is where this architecture would need work before 100 stations became routine rather than a stress case.

Performance carries 37–47x headroom against the customer-facing budgets, so latency is not the binding constraint for this architecture. That is a finding, not a reason to loosen the budgets: the budgets describe what the business needs, and the headroom is what the monolith buys us before network hops start consuming it.

### Group and DeDuplicate

#### Category Groupings

Operational:
- Availability
- Continuity
- Performance
- Recoverability
- Reliability
- Robustness
- Scalability

Structural:
- Configurability
- Extensibility
- Installability
- Maintainability
- Upgradeability

Cross-Cutting:
- Security
- Supportability
- Usability

#### Implementation Groupings (what will benefit from each other)

- **Continuity + Recoverability + Upgradeability** — all satisfied by the same decision: durable state in PostgreSQL with a deterministic rebuild path. One mechanism, three characteristics.
- **Availability + Scalability + Performance** — all served by stateless workers behind shared state. Because no worker holds session state, adding workers is the lever for all three at once (and is why the analytics window had to live in the database rather than in process memory).
- **Reliability + Robustness + Supportability** — the same design produces all three: doing the stock decrement in one transaction makes it correct, makes contention resolve deterministically, and makes the resulting `409` a meaningful signal instead of noise.
- **Configurability + Installability + Usability** — one reset command plus environment-variable configuration is what makes runs reproducible and comparable week to week.

### Trade Offs

- **Performance vs. reliability.** Every lock and conditional check costs latency. The mock server is the extreme case: it decrements with `Math.max(0, current - 1)` and no database, reaching 2 624 tx/s — but it silently loses units and fails the graded invariant the moment anything depletes. Our correct implementation runs 788 tx/s on the same hardware. **Correctness costs roughly 3.3x throughput here, and it is worth it** — a checkout system that charges for unavailable stock is not a faster system, it is a broken one.
- **Scalability vs. simplicity.** More workers and richer concurrency control raise throughput but add operational and debugging burden. Async is mandatory for I/O-bound request handling, yet async control flow is harder to reason about than straight-line procedural code, and the failure modes (a blocked event loop, a connection pool bound to the wrong loop) are less obvious.
- **Latency vs. durability.** `synchronous_commit = off` removes an fsync from the completion path at the cost of a sub-second RPO on hardware failure. Fully synchronous commits would make the benchmark measure disk fsync latency rather than the architecture.
- **Maintainability vs. optimization.** Hand-written SQL on the two hot paths is faster and expresses the locking more clearly than ORM unit-of-work, but it is a second way of talking to the database that has to be kept consistent with the models. Cold paths stay on the ORM.
- **Maintainability vs. supportability.** Delegating to well-built libraries beats writing our own, but each dependency is a maintenance and upgrade tax.

### Top Ranked 3

1. **Reliability/safety.** Inventory correctness, transaction consistency, and data security are the core business requirements. Customers paying for goods they do not receive, erroneous transactions, or leaked data are existential, not inconvenient. This is also the only characteristic on this list with zero tolerance — and, per the trade-off above, the one we deliberately spent throughput to guarantee.
2. **Performance.** The service must stay responsive across both load profiles; customers tolerate a short wait and abandon a long one. Measurement shows this is currently satisfied with wide margin, which means it is not the constraint *this* week — but it is the characteristic most likely to erode first as later weeks add network hops between services.
3. **Scalability.** Growing station count should not require a redesign. Small-scale excellent service is still a viable business, so this ranks below the first two — but the 17.7x completion degradation already measured at 100 stations is the concrete warning sign to carry into the distributed weeks.
