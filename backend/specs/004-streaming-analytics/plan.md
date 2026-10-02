# Plan: Streaming Pipeline Architecture

**Date**: 2026-10-02

---

## Context

**Tech Stack**:
- Python 3.11 with asyncio (single-process concurrency)
- FastAPI + SQLAlchemy (async ORM)
- PostgreSQL 17 (database)
- Java load client (generates scan events)

**Current State**:
- Spec 003 implements a batch-based pipeline that queries the database at slide boundaries
- This spec (004) refactors to a streaming architecture where scans flow continuously through filters in real-time
- Both approaches should produce identical rankings; streaming is more responsive and demonstrates true pipeline parallelism

**Key Insight from Assignment**: 
> "think of the manner in which the windowed analytics operates"
This hints at a *streaming hopping window* pattern where data flows through the pipeline as events arrive, not as batched SQL queries.

---

## Architecture Overview

### Four-Stage Filter Pipeline

```
Scan Event (from transaction_item table insert)
    ↓
[IngestFilter]
  - Buffers incoming scans in circular buffer (size: 1000)
  - Emits SLIDE event when 500 new scans arrive
    ↓ (via ingest_queue)
[WindowFilter] 
  - Receives SLIDE event
  - Maintains the 1000-scan window
  - Discards oldest 500 scans on each slide
  - Emits window data downstream
    ↓ (via window_queue)
[RankingFilter]
  - Receives the 1000-scan window
  - Ranks items by scan count (descending)
  - Emits RankedData (top 200 items)
    ↓ (via ranking_queue)
[OutputFilter]
  - Receives RankedData
  - Writes to popular_window_snapshot table
  - Commits transaction (non-blocking)
```

### Concurrency Model

- All four filters run concurrently in a single event loop via `asyncio.gather()`
- Filters communicate via asyncio.Queue (FIFO, unbounded by default)
- Sentinels (TERMINATE) signal pipeline shutdown

### Data Flow Pattern

1. **Ingest Phase**: Incoming scans accumulate in a buffer
2. **Slide Phase** (every 500 scans):
   - Discard oldest 500
   - Keep most recent 1000
   - Emit window to downstream filters
3. **Ranking Phase**: Receive window, compute ranking
4. **Output Phase**: Persist to database (non-blocking)

---

## Implementation Strategy

### Phase 1: Refactor IngestFilter (Real-Time Scan Buffering)

**File**: `backend/app/analytics/pipeline.py`

Currently, IngestFilter queries the database once. It should instead:
1. Hook into the transaction completion path
2. Receive scan events as `ScanData(sku, scan_id)` messages
3. Maintain a deque of 1000 scans
4. Emit SLIDE event when count reaches 500
5. Continue buffering the next 500

**Challenge**: Currently, scan events don't flow through analytics; they go directly to the database. We need to either:
- **Option A** (Cleaner): Inject a hook into `transactions/service.py` to emit scan events to the pipeline
- **Option B** (Simpler for now): Have IngestFilter query the database periodically (polling) and ingest new scans since last query

For this phase, we'll start with **Option B** (polling) to minimize changes to the transaction layer. Once the pipeline architecture is proven, we can migrate to **Option A** for true streaming.

### Phase 2: Implement WindowFilter (Sliding Window Maintenance)

**File**: `backend/app/analytics/pipeline.py`

WindowFilter receives ScanData items and:
1. Maintains a deque of exactly 1000 scans
2. Counts incoming scans
3. When count reaches 500, emits a SlideEvent with the current window
4. Discards oldest 500, continues buffering

### Phase 3: RankingFilter (Unchanged from Spec 003)

Receives window data, produces ranking. Same as before.

### Phase 4: OutputFilter (Unchanged from Spec 003)

Receives RankedData, writes to database. Same as before.

### Phase 5: Integration & Testing

- Write integration tests that verify all four filters work together
- Write unit tests for each filter in isolation
- Run load tests to verify streaming model handles concurrency
- Measure latency: scan arrival → window slide → ranking output → database write

---

## Design Decisions

| Decision | Rationale |
|:---|:---|
| Asyncio (not threads) | GIL-friendly for I/O-bound operations; single event loop simplifies synchronization |
| In-memory window (not DB) | 50 KB memory cost; much faster to slide; reduces database load |
| Polling (not hooks) | Minimal changes to existing code; can be optimized to event-driven later |
| Sentinel-based termination | Clean shutdown without exception handling; works with asyncio.Queue |
| Non-blocking database writes | OutputFilter commits in background; doesn't block RankingFilter |

---

## File Structure

```
backend/
  app/
    analytics/
      pipeline.py              # IngestFilter, WindowFilter, RankingFilter, OutputFilter
      scheduler.py             # Calls pipeline.run_streaming() instead of recompute_windowed()
      popular_items.py         # Read-path (unchanged)
      low_stock.py             # (unchanged)
    transactions/
      service.py               # (unchanged for now; can add hooks later)
    db/
      analytics_repo.py        # (unchanged)
  specs/
    004-streaming-analytics/
      spec.md                  # (this file)
      plan.md                  # (this file)
      research.md              # Design decisions & trade-offs
      data-model.md            # Data structures & schemas
      quickstart.md            # Validation scenarios
      tasks.md                 # Implementation tasks
  tests/
    integration/
      test_pipeline_streaming.py  # End-to-end pipeline tests
    unit/
      test_pipeline_filters.py    # Individual filter tests
```

---

## Success Measures

1. **Correctness**: Pipeline produces identical ranking as spec 003 (compare snapshots)
2. **Responsiveness**: Ranking updates reflect latest scans (window metadata shows progression)
3. **Concurrency**: Stress test (100 stations, 120s) with no deadlock or queue overflow
4. **Latency**: Slide → Output < 100 ms (p95)
5. **Throughput**: Process ≥ 100 scans/second at p95

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|:---|:---|:---|
| In-memory window reset on restart | Rankings lost | Document in troubleshooting; add persistence in future |
| Queue overflow under extreme load | Pipeline stalls | Monitor queue depth; add backpressure logic if needed |
| Database write blocks event loop | Latency spikes | Use `asyncio.to_thread()` for commits if needed |
| Polls miss scans between queries | Ranking gap | Add offset tracking to ensure no duplicates/gaps |

---

## Timeline

- **Phase 1-2** (Ingest + Window filters): 2-4 hours (core streaming logic)
- **Phase 3-4** (Ranking + Output): 1 hour (reuse from spec 003)
- **Phase 5** (Integration, Testing, Load Test): 2-3 hours
- **Total**: ~6-8 hours for MVP (streaming pipeline working end-to-end)

---

## Next Steps

1. Create data-model.md defining ScanData, WindowData, RankedData, SlideEvent
2. Create research.md documenting design trade-offs
3. Create quickstart.md with validation scenarios
4. Create tasks.md with implementation tasks (dependent on research/data-model)
5. Begin implementation (Phase 1 onwards)
