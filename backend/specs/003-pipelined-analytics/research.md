# Research: Pipelined Analytics Architecture

**Phase 0 Output**: Research findings and decisions for pipelined analytics implementation

**Date**: 2026-10-02

---

## R1: Asyncio Queue-Based Communication

**Decision**: Use Python `asyncio.Queue` for inter-filter communication

**Rationale**: 
- Asyncio queues are built-in to Python's standard library
- Integrate seamlessly with FastAPI's async/await model (consistent with backend architecture)
- Provide natural backpressure handling through queue sizes
- Support streaming semantics with sentinel values (None) for end-of-stream

**Alternatives Considered**:
- **Threading queues (queue.Queue)**: Would require thread-pool executors; adds complexity with Python's GIL limitations; inconsistent with FastAPI's async patterns
- **Message brokers (Redis, RabbitMQ)**: Over-engineered for single-machine in-process pipeline; adds operational overhead; not needed for background task
- **Generator pipelines**: Simpler but less clear separation of concerns; harder to test filters in isolation

**Conclusion**: Asyncio queues are the appropriate choice for this architecture.

---

## R2: Filter Concurrency Model

**Decision**: Use `asyncio.gather()` to run all filter coroutines concurrently

**Rationale**:
- Asyncio.gather runs coroutines concurrently (with shared event loop, not OS threads)
- Natural Python idiom for concurrent async work
- Simple to reason about: all filters start together, pipeline completes when all finish
- Error propagation is automatic (one filter exception fails the entire gather)

**Alternatives Considered**:
- **Sequential filter invocation**: No parallelism benefit; defeats the purpose of pipelining
- **Thread pool**: Would require thread-safe queues; Python GIL serializes actual computation; unnecessary complexity
- **asyncio.create_task()**: More complex cancellation/error handling; gather is simpler for this use case

**Conclusion**: Asyncio.gather provides the right concurrency model for the pipeline.

---

## R3: Sentinel Value Pattern for Stream End

**Decision**: Use `None` as sentinel value to signal end-of-stream

**Rationale**:
- Standard Python pattern for signaling stream termination
- Unambiguous: None cannot be a valid data item in the pipeline
- Simple to implement: each filter checks `if data is None: break`
- Works naturally with asyncio queues

**Alternatives Considered**:
- **Exception throwing**: Could signal completion via exception; harder to distinguish from actual errors
- **Custom sentinel class**: Adds unnecessary abstraction; None is simpler and more Pythonic
- **Queue sentinel with length tracking**: More complex; None is sufficient

**Conclusion**: None sentinel is the standard and simplest approach.

---

## R4: Ranking Depth Limit (200 items)

**Decision**: Rank and store top 200 items (RANKING_DEPTH = 200)

**Rationale**:
- Inherited from existing synchronous implementation (popular_items.py:21)
- Balances completeness (covers any reasonable API query limit) with storage/performance
- JSON blob size remains small even with metadata (< 10KB for typical catalog)

**Alternatives Considered**:
- **Rank all items**: Unnecessary storage and computation; queries rarely need beyond top 100
- **Configurable depth**: Would require additional configuration; fixed value is simpler
- **Dynamic depth based on window size**: Overcomplicates logic; fixed value is sufficient

**Conclusion**: RANKING_DEPTH = 200 is appropriate and consistent with existing implementation.

---

## R5: Database Access Pattern

**Decision**: Reuse existing `db.analytics_repo` functions for all database operations

**Rationale**:
- IngestFilter can reuse `window_counts()` for reading scans
- OutputFilter can reuse `upsert_snapshot()` for writing
- Maintains layer boundary: analytics layer never includes SQL directly
- Avoids code duplication and ensures consistency

**Alternatives Considered**:
- **Direct SQL in filters**: Violates layer boundaries established in layered architecture spec
- **New repository functions**: Unnecessary; existing functions are sufficient
- **SQLAlchemy queries in filters**: Mixes data access with business logic; not aligned with architecture

**Conclusion**: Reuse analytics_repo functions; no new data-access code needed.

---

## R6: Error Handling Strategy

**Decision**: Log exceptions and propagate up; let error handling in scheduler prevent surface as request errors

**Rationale**:
- Pipeline runs off critical path (background task via scheduler)
- Scheduler already has try/except wrapping (scheduler.py:35-36)
- Logging provides observability without blocking requests
- Consistent with "never let background work surface as request error" principle

**Alternatives Considered**:
- **Silent failure**: No observability; makes bugs hard to diagnose
- **Retry logic**: Unnecessary complexity; scheduler will retry on next slide boundary
- **Partial recovery**: Difficult to define safe partial states; cleaner to fail atomically

**Conclusion**: Log and propagate errors; rely on scheduler's exception handling.

---

## Summary of Decisions

| Research Item | Decision | Confidence |
|---|---|---|
| Queue communication | asyncio.Queue | High |
| Concurrency model | asyncio.gather() | High |
| Stream termination | None sentinel | High |
| Ranking depth | 200 items | High |
| Database access | Reuse analytics_repo | High |
| Error handling | Log and propagate | High |

**Status**: All clarifications resolved. Ready for Phase 1 design.
