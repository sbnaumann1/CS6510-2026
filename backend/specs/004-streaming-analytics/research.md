# Research: Design Decisions for Streaming Pipeline

**Date**: 2026-10-02

---

## R1: Streaming vs. Batch Processing

**Question**: Should scans flow continuously through the pipeline (streaming), or should we fetch them in batches from the database (batch)?

**Decision**: **STREAMING** — Process scans as they arrive; maintain in-memory window.

**Rationale**:
- **Streaming (chosen)**:
  - Reflects the assignment's hint: "think of the manner in which the windowed analytics operates"
  - More responsive: ranking updates continuously, not at fixed intervals
  - Demonstrates true pipeline parallelism: filters work on different data simultaneously
  - Lower database load: no SQL queries during window slides
  - In-memory buffer (50 KB) is negligible
  
- **Batch (Spec 003)**:
  - Simpler to implement (reuse existing SQL queries)
  - Decouples analytics from transaction processing
  - But: misses the point of a "pipeline" (batch is linear, not parallel)
  - Higher database load: SQL query every 500 scans

**Trade-off**: Streaming requires coordinating scan injection into the pipeline; batch is simpler but less responsive.

**Chosen**: Streaming (per assignment expectations)

---

## R2: Scan Ingestion Source

**Question**: Where do scans come from?

**Options**:
- **A. Event Hook** (True Streaming): Inject scans from `transactions/service.py` as they're committed
- **B. Polling** (Quasi-Streaming): Periodically query database for new scans
- **C. Batch Query** (Hybrid): Query at slide boundaries (Spec 003 approach)

**Decision**: **START WITH B (POLLING)**, migrate to A later.

**Rationale for B (Polling)**:
- Minimal changes to transaction layer
- Scans flow through pipeline in near real-time (polling interval << 500-scan duration)
- Easy to test in isolation
- Can measure polling overhead

**Migration Path to A (Event Hooks)**:
- Once pipeline is validated, inject ScanData directly into IngestFilter queue from `transactions/service.py`
- Requires minimal code: one line in `complete_transaction()` to emit event
- But adds coupling between layers (per spec 002 layer boundaries)

**Chosen**: B initially; A as an optimization.

---

## R3: In-Memory Window vs. Database Window

**Question**: Should the 1000-scan window be maintained in-memory or queried from the database?

**Decision**: **IN-MEMORY** (circular buffer in IngestFilter)

**Rationale**:
- Size: 1000 scans × ~50 bytes each = ~50 KB (negligible)
- Speed: `deque.popleft()` is O(1); database query is I/O
- Pipeline responsiveness: no latency waiting for SQL
- Database simplicity: no additional table or indexes needed

**Trade-off**: Window is lost on restart (not persistent across app restarts)

**Mitigation**: Document in troubleshooting; persistence can be added later if needed

**Chosen**: In-memory with deque (collections.deque)

---

## R4: Window Size and Slide Interval

**Question**: What should the window size and slide interval be?

**Decision**: 
- **Window size**: 1000 scans
- **Slide interval**: 500 scans

**Rationale**:
- Matches existing assignment requirements (FR1, FR2)
- Sliding window (50% overlap) smooths transitions between rankings
- 500-scan interval = recompute every ~5 seconds at 100 scans/sec
- Top 1000 items sufficient to rank ~200 items in output

**Alternatives Considered**:
- Window 2000, slide 1000: Lower overhead, but stale ranking by 2x
- Window 500, slide 250: More responsive, but higher CPU for ranking

**Chosen**: 1000 / 500 (standard hopping window)

---

## R5: Queue Unbounded vs. Bounded

**Question**: Should asyncio.Queue be unbounded (default) or bounded?

**Decision**: **UNBOUNDED** (default `asyncio.Queue()`)

**Rationale**:
- Allows producer to emit data without blocking
- Pipeline handles backpressure gracefully: if OutputFilter is slow, queue backs up but doesn't deadlock
- Bounded queues can cause deadlock if producer/consumer rates don't match

**Monitoring**: Log queue depths periodically to detect bottlenecks.

**Chosen**: Unbounded (default)

---

## R6: Sentinel-Based Shutdown vs. Exception-Based

**Question**: How should the pipeline shut down cleanly?

**Decision**: **SENTINEL-BASED** (emit TERMINATE message to trigger graceful shutdown)

**Rationale**:
- All filters see the same TERMINATE signal
- No exceptions cluttering logs
- Clear semantics: TERMINATE means "finish current work, then exit"
- Works well with asyncio queues

**Alternative**: 
- Raise exception in IngestFilter (less clean)
- Use asyncio.CancelledError (harder to debug)

**Chosen**: Sentinel-based with TERMINATE message

---

## R7: Database Write Blocking

**Question**: Should OutputFilter's database commit block RankingFilter's next computation?

**Decision**: **NON-BLOCKING** — OutputFilter writes to database in background if needed.

**Rationale**:
- If OutputFilter's `session.commit()` is slow (network I/O), it blocks the pipeline
- Better: queue writes if needed, or use `asyncio.to_thread()` for blocking operations
- Ensures RankingFilter can proceed with next slide while database is being written

**Implementation**:
```python
# Within OutputFilter
await asyncio.to_thread(lambda: session.commit())  # Non-blocking commit
```

Or: Queue the write if `session.commit()` becomes a bottleneck.

**Chosen**: Non-blocking via `asyncio.to_thread()` if needed; measure first

---

## R8: Error Handling Strategy

**Question**: What happens if a filter encounters an error (e.g., database connection lost)?

**Decision**: **LOG AND CONTINUE** — Don't crash; emit sentinel to stop gracefully.

**Rationale**:
- Pipeline is off critical path (not on the request/response path)
- Losing a ranking update is acceptable; next slide will compute new one
- Crashing the app for an analytics error is excessive

**Implementation**:
- Wrap each filter's `run()` in try/except
- Log exception with full traceback
- Emit sentinel to downstream filters
- Continue processing next slide

**Chosen**: Log and continue (non-blocking error handling)

---

## R9: Comparison with Spec 003

| Aspect | Spec 003 (Batch) | Spec 004 (Streaming) |
|:---|:---|:---|
| Data source | SQL query at slide boundary | Continuous scan events + polling |
| Window location | Computed from database | In-memory circular buffer |
| Ranking computation | Query result is pre-ranked | Rank from full window |
| Latency | 1 SQL query per 500 scans | In-memory deque ops (microseconds) |
| Pipeline parallelism | Limited (ranking already done by SQL) | True parallelism (all filters work simultaneously) |
| Complexity | Simpler (fewer components) | More components, but more modular |
| Performance | Database-limited | Memory/CPU-limited |

**Key Insight**: Spec 004 demonstrates *true* pipeline architecture; Spec 003 was primarily a code refactor of the existing batch approach.

---

## R10: Testing Strategy

**Question**: How should we validate the streaming pipeline?

**Decision**: 
1. **Unit tests**: Mock queues, test each filter in isolation
2. **Integration tests**: Run full pipeline with real database, verify output
3. **Load tests**: Stress test with 100 stations, verify no deadlock
4. **Comparison test**: Verify spec 004 ranking matches spec 003 ranking on same dataset

**Chosen**: All four levels

---

## Open Questions (For Later)

- **Q1**: Should IngestFilter hook into transaction commits (true event-driven)?
  - Answer: Yes, but after MVP is working
  
- **Q2**: Should we persist the in-memory window to disk?
  - Answer: Not for MVP; document restart behavior
  
- **Q3**: Can filters be distributed across processes?
  - Answer: Not with asyncio; would need multiprocessing or message broker (out of scope)
  
- **Q4**: Should we support horizontal scaling (multiple pipeline instances)?
  - Answer: Not for MVP; single instance per app is sufficient

---

## Summary

The streaming pipeline in Spec 004:
- Maintains in-memory 1000-scan window
- Slides every 500 scans
- Emits RankedData downstream via asyncio queues
- All filters run concurrently
- Non-blocking error handling
- Designed for true pipeline parallelism, not just code organization

This approach aligns with the assignment's hint about "the manner in which the windowed analytics operates" — a continuous hopping-window stream processing pattern.
