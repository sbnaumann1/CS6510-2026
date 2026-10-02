# Feature Specification: Pipelined Analytics Architecture

**Feature Branch**: `003-pipelined-analytics`

**Created**: 2026-10-02

**Status**: Draft

**Input**: Refactor the popular items windowed analytics functionality into a pipelined architecture. The analytics currently performs a hopping window computation over scan sequences to rank popular items. This should be decomposed into independent filter stages (ingest, aggregate, rank, output) connected by asyncio queues for asynchronous inter-stage communication. Each filter runs concurrently and processes data independently. The pipeline should maintain correctness of the windowed ranking computation while naturally decomposing the workflow into stages.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Compute Popular Items Rankings (Priority: P1)

**Actors**: Analytics system, periodic recompute scheduler

The system needs to efficiently recompute the popular items window snapshot whenever a slide boundary is crossed. The computation should decompose naturally into independent stages that can process data concurrently without blocking each other.

**Why this priority**: This is the core functionality of the feature. The system must correctly rank items by scan count within a time window and persist the results for API consumers.

**Independent Test**: Can be fully tested by running the analytics recompute pipeline and verifying that:
1. The correct window bounds are computed
2. Items are ranked by scan count in descending order
3. The snapshot is persisted to the database
4. The result matches the previous synchronous implementation

**Acceptance Scenarios**:

1. **Given** a configured window size (e.g., 1000 scans), **When** the system reaches a slide boundary, **Then** the pipeline computes the window bounds correctly and identifies the top 200 scanned items
2. **Given** completed pipeline execution, **When** the popular items API is called, **Then** it returns the ranked items matching the snapshot computed by the pipeline
3. **Given** concurrent scan activity, **When** the pipeline runs, **Then** it produces the same ranking as the previous synchronous implementation

---

### User Story 2 - Handle High-Concurrency Analytics (Priority: P2)

**Actors**: Multiple concurrent checkout stations, analytics pipeline

Under high concurrent load (e.g., 100+ stations), the analytics computation should remain responsive and not block request processing. Filters should process data independently, allowing work to overlap naturally.

**Why this priority**: The system must scale to handle busy retail operations. Pipeline decomposition enables concurrent filter processing and prevents the analytics recompute from becoming a bottleneck.

**Independent Test**: Can be fully tested by running 100+ concurrent stations for 120+ seconds and verifying:
1. The pipeline completes within reasonable time even under high load
2. The popular items snapshot is correctly updated
3. Request processing is not blocked by analytics computation

**Acceptance Scenarios**:

1. **Given** 100 concurrent checkout stations, **When** the pipeline runs during high activity, **Then** filters process data concurrently and the pipeline completes successfully
2. **Given** high concurrent scan volume, **When** the ranking filter processes items, **Then** it produces a correct ranking despite concurrent updates to earlier stages
3. **Given** pipeline execution under load, **When** the output filter persists the snapshot, **Then** the database commit succeeds and consumers see the updated ranking

---

### User Story 3 - Decompose Analytics Into Independent Filters (Priority: P3)

**Actors**: Developers, system maintenance

The analytics code should be organized as independent filter stages with clear separation of concerns. Each filter should handle one aspect of the computation (ingestion, aggregation, ranking, or persistence).

**Why this priority**: This improves code maintainability and testability. Independent filters can be tested, optimized, and modified in isolation.

**Independent Test**: Can be fully tested by:
1. Testing each filter independently with mock input/output queues
2. Verifying that filters can be composed in different orders (if needed)
3. Confirming that data flowing through the pipeline produces the expected result

**Acceptance Scenarios**:

1. **Given** the IngestFilter, **When** provided window bounds, **Then** it reads scans from the database and emits aggregated data to the output queue
2. **Given** the RankingFilter, **When** provided aggregated scan counts, **Then** it ranks items by count and emits ranked data with metadata
3. **Given** the OutputFilter, **When** provided ranked data, **Then** it persists the snapshot to the database and commits the transaction

---

### Edge Cases

- What happens when the window contains zero scans? (Should return empty ranking)
- How does the system handle database contention when multiple workers try to recompute simultaneously? (Advisory lock ensures only one worker proceeds)
- What if a filter encounters a database error mid-execution? (Error is logged, exception propagates through pipeline stages)
- How are pipeline queues sized to prevent memory exhaustion? (Queue sizes are unbounded but limited by the number of items in a window)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST decompose popular items analytics into independent filter stages (ingest, aggregate, rank, output)
- **FR-002**: Filters MUST communicate via asyncio queues with sentinel values signaling end-of-stream
- **FR-003**: IngestFilter MUST read scan sequences from the database within configured window bounds
- **FR-004**: AggregationFilter MUST buffer and forward aggregated scan count data between ingest and ranking stages
- **FR-005**: RankingFilter MUST rank items by scan count and include window metadata (bounds, size, interval)
- **FR-006**: OutputFilter MUST persist the computed snapshot to the popular_window_snapshot table
- **FR-007**: OutputFilter MUST commit the database transaction after persisting the snapshot
- **FR-008**: All filters MUST run concurrently via asyncio.gather()
- **FR-009**: The pipeline MUST maintain correctness of the windowed ranking computation (same results as synchronous implementation)
- **FR-010**: The scheduler MUST invoke the pipelined recompute function instead of the synchronous recompute function
- **FR-011**: Error handling MUST log exceptions without surfacing them as request errors (off critical path)

### Key Entities

- **Window**: Time-windowed segment of scan sequences defined by (start_seq, end_seq)
- **ScanData**: Individual scan record with SKU and scan sequence number
- **AggregatedData**: Scan counts per SKU within a window
- **RankedData**: Final ranked list of items with window metadata
- **Pipeline Stage**: Independent filter with input queue, processing logic, and output queue

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Pipeline produces identical popular items ranking as the synchronous implementation when given the same input window
- **SC-002**: Pipeline executes successfully under load (100+ concurrent stations, 120+ seconds) without deadlock or corruption
- **SC-003**: All four filter stages execute concurrently (parallelism can be observed via logging or metrics)
- **SC-004**: Popular items API returns correct ranking immediately after pipeline completion
- **SC-005**: Low-stock alerts are computed correctly alongside popular items (no degradation in dependent functionality)
- **SC-006**: Analytics recompute does not block request processing (remains off critical path)

## Assumptions

- **Architecture**: The project uses Python with asyncio for asynchronous operations
- **Database**: PostgreSQL with async driver (asyncpg) is available and supports concurrent access
- **Window Configuration**: Popular window size and slide interval are already configured in settings
- **Data Model**: Transaction items are indexed by scan sequence ID and queryable within window bounds
- **Existing APIs**: The analytics repository (db.analytics_repo) provides query functions that can be reused
- **Backward Compatibility**: The pipeline behavior must be identical to the synchronous implementation for correctness
- **Scope**: Only the popular items analytics are being pipelined; low-stock alerts and other analytics remain unchanged
- **Testing**: Load testing with the existing Java load client (100+ stations) is sufficient to validate high-concurrency behavior
