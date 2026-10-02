# Spec: Streaming Pipeline for Popular Items Analytics

**Date**: 2026-10-02

**Revision**: 004 (Streaming Architecture - Data Flows In Real-Time)

---

## Overview

The popular items analytics will be refactored into a **streaming multi-stage pipeline** where scan events flow continuously through independent filters connected by asyncio queues. Unlike the batch-based approach (spec 003) that queries the database at slide boundaries, this pipeline processes scans as they arrive in real-time, maintains an in-memory sliding window of the most recent 1000 scans, and computes rankings dynamically as the window slides.

### Key Difference from Spec 003

- **Spec 003 (Batch)**: Every 500th scan triggers a SQL query to fetch all scans in the window from the database
- **Spec 004 (Streaming)**: Scan events flow continuously through the pipeline; the window is maintained in-memory; every 500 new scans, the window slides and rankings are recomputed

---

## User Stories

### US1: Real-Time Scan Ingestion (Priority: P1 🎯 MVP)

**As a** system operator,  
**I want** scan events to be processed through a streaming pipeline immediately as they arrive,  
**So that** the popular items ranking reflects current customer behavior without query latency.

**Acceptance Criteria**:
- Scan events are buffered in an IngestFilter that maintains a sliding window of 1000 scans
- Every 500 new scans, a SLIDE event triggers downstream filters
- Window is maintained in-memory; no database queries during slide
- RankingFilter receives window data and produces a new ranking
- OutputFilter persists ranking to database
- All filters are non-blocking; pipeline does not halt on database writes

---

### US2: Independent Concurrent Filters (Priority: P2)

**As a** developer,  
**I want** to be able to test and modify individual filters independently,  
**So that** the pipeline is maintainable and filters can be optimized separately.

**Acceptance Criteria**:
- IngestFilter can be tested with mock scan events
- WindowFilter can be tested with mock input/output queues
- RankingFilter can be tested with mock window data
- OutputFilter can be tested with mock database
- Filters use standard asyncio.Queue operations only
- No filter depends on implementation details of adjacent filters

---

### US3: Scale to High Concurrency (Priority: P3)

**As a** system operator,  
**I want** the streaming pipeline to handle 100+ concurrent stations without deadlock or queue overflow,  
**So that** analytics remain responsive under peak load.

**Acceptance Criteria**:
- Pipeline processes 100,000+ scans per minute without queue backing up
- No deadlocks when multiple recompute cycles are in flight
- Window slides smoothly every 500 scans with <100ms latency
- Output writes do not block the pipeline (database commits are non-blocking)

---

## Functional Requirements

| ID | Requirement | Priority |
|:---|:---|:---|
| **FR1** | IngestFilter maintains a circular buffer of exactly 1000 scans in memory | P1 |
| **FR2** | IngestFilter emits a SLIDE event when the 500th new scan arrives | P1 |
| **FR3** | WindowFilter discards the oldest 500 scans when it receives a SLIDE | P1 |
| **FR4** | RankingFilter receives a 1000-scan window and ranks items by scan count (descending) | P1 |
| **FR5** | RankingFilter emits RankedData containing top 200 items with window metadata | P1 |
| **FR6** | OutputFilter writes RankedData to `popular_window_snapshot` table atomically | P1 |
| **FR7** | All filters are connected by asyncio.Queue for inter-filter communication | P1 |
| **FR8** | Pipeline can be stopped gracefully via a TERMINATE sentinel | P2 |
| **FR9** | Pipeline logs start, slide events, and rankings for observability | P2 |
| **FR10** | Database commit in OutputFilter does not block RankingFilter | P2 |
| **FR11** | Pipeline recovers gracefully if database write fails (log error, continue) | P3 |

---

## Success Criteria

| ID | Criterion | Verification |
|:---|:---|:---|
| **SC1** | Streaming pipeline produces same ranking as batch approach | Integration test with seeded data |
| **SC2** | Pipeline processes 100,000 scans/min without queue overflow | Load test with 100 stations × 120s |
| **SC3** | All filters are independently testable with mock queues | Unit tests pass in isolation |
| **SC4** | Ranking updates every 500 scans (visible in API response) | Observe window metadata changes |
| **SC5** | No deadlocks under high concurrency | Stress test with no timeout/error |
| **SC6** | Database writes do not block incoming scans | p95 latency unaffected during recompute |

---

## Assumptions

1. **Scan order is globally consistent**: `transaction_item.id` is the authoritative scan sequence; all filters agree on ordering
2. **In-memory window is acceptable**: 1000 scans × ~50 bytes per scan = ~50 KB, well within memory budget
3. **Ranking depth is 200 items**: Sufficient for API display and analytics queries
4. **Slide boundary is every 500 scans**: Balances responsiveness vs. update frequency
5. **Database writes can be async**: OutputFilter's commit does not block the pipeline

---

## Edge Cases & Constraints

### Edge Cases

| Case | Handling |
|:---|:---|
| First 500 scans | Buffer them; emit SLIDE when count reaches 500 |
| Fewer than 1000 scans total | Rank whatever is available; window_size reflects actual count |
| High scan rate > ingestion capacity | Queue backs up (OS-level backpressure); no data loss |
| Database commit fails | Log error; continue processing; retry on next slide |
| Pipeline shutdown mid-recompute | Finish current slide; then exit |

### Constraints

- **Memory**: Window buffer = 1000 scans × 50 bytes ≈ 50 KB
- **Latency**: Slide → RankingFilter → OutputFilter < 100 ms (target)
- **Throughput**: Must sustain ≥ 100 scans/second at p95
- **Concurrency**: All four filters run simultaneously via `asyncio.gather()`

---

## Out of Scope

- Distributed pipeline across multiple processes (single-process asyncio only)
- Persistence of in-memory window to disk (window resets on app restart)
- Historical ranking snapshots (only latest snapshot persists)
- Real-time subscriptions to ranking updates (polling via API only)
