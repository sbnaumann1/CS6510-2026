# Persistent Streaming Pipeline for Popular-Items Analytics

## Overview

Since spec 005 (service-based architecture), popular-items analytics runs inside the
**Analytics Service**. It is **one persistent streaming pipeline** for the life of the
service. It is no longer a fresh pipeline run triggered every 500 scans.

The pipeline learns about new scans only by **tailing the shared scan record**
(`transaction_item`, whose `id` is the global scan sequence) in the centralized
database. The Checkout Service writes those rows and never calls, or even knows about,
Analytics.

Design and rationale: `backend/specs/005-service-architecture/research.md` R5–R6 and
`data-model.md` §3. Code: `backend/services/analytics/src/analytics_service/pipeline.py`
(filters) and `supervisor.py` (lifecycle).

```text
TailIngest ─q1─▶ WindowFilter ─q2─▶ AggregationFilter ─q3─▶ RankingFilter ─slot─▶ OutputFilter
 (DB tail)       (slide by id)      (running counts)        (top-200)              (latest-wins, commit)
```

Every filter is a long-running loop that keeps its state across windows.
`TERMINATE` flows downstream only at shutdown, never between windows.

## Filters

### 1. TailIngest (replaces the per-run IngestFilter)
- **Input**: the shared database. It polls `SELECT id, sku FROM transaction_item WHERE id > :last_id ORDER BY id LIMIT 1000`.
- **Output**: `ScanData(scan_id, sku)` in id order, plus `Watermark(boundary)` once every id ≤ boundary is accounted for.
- **Gap rule**: concurrent scans commit out of id order, so the tail keeps a *contiguous* watermark `last_id`. Rows above a hole wait until the hole fills. A hole still open after 1 s (`POPULAR_GAP_GRACE_MS`) is a sequence value that never committed: the tail logs `gap skipped id=<n>` and moves on.
- **Pacing**: full batches are read back to back, so a backlog drains at DB speed. It sleeps `POPULAR_POLL_MS` (100 ms) only when caught up or waiting on a hole.

### 2. WindowFilter
- Keeps a deque of recent scans. The window is defined by **scan id**, not arrival count.
- On `Watermark(b)` (every multiple of 500) it emits `Slide(b−1000, b, added, evicted)`: the SKUs entering and leaving the 1000-scan window.

### 3. AggregationFilter
- Keeps **one running `Counter`** for the pipeline's lifetime. Each slide applies +1 per added scan and −1 per evicted scan, which is O(500) work per slide rather than a fresh count of 1000.
- It emits `WindowCounts`.

### 4. RankingFilter
- Sorts by `(−count, sku)`, so ties break by SKU, matching the SQL `window_counts`. It keeps the top 200 and emits `RankedData`.

### 5. OutputFilter
- Reads from a **latest-wins slot**: if several windows are ready at once, only the newest is written, because clients only ever see the latest snapshot.
- Upserts `popular_window_snapshot`, **commits each window**, and logs `window published start=<s> end=<e>`.

## Lifecycle (supervisor.py)

1. **Leader lock**: a PostgreSQL session-level advisory lock on a dedicated connection. At most one pipeline runs across all Analytics replicas. Standbys retry every 2 s.
2. **Bootstrap**:
   - Read the max scan id and set `b0` to the latest 500-boundary.
   - Tail from `b0 − 1000`, so the first window published is `b0` and complete.
   - Log `pipeline started bootstrap_from=<id>`.
   - A restart therefore catches up from the database, and nothing is lost while Analytics is down.
3. **Errors**: any filter exception cancels all filters, and the pipeline re-bootstraps after 1 s.
4. **Shutdown**: the tail is stopped, then `TERMINATE` drains downstream.

## Correctness checks

- `tests/integration/test_persistent_pipeline.py`: with 8 concurrent scanners, every published window equals a direct SQL count over the same bounds, and only one pipeline instance exists for the run.
- `tests/integration/test_pipeline_restart.py`: after a restart, the first window is the complete latest one.
- `tests/unit/test_tail_ingest_gaps.py`, `tests/unit/test_pipeline_filters.py`: each filter in isolation, including late commits and permanent gaps.

## History

- **Spec 003**: a 4-stage batch pipeline, run per trigger.
- **Spec 004**: a streaming pipeline, still triggered in-process by the scan route every 500 scans and rebuilt each time.
- **Spec 005**: the trigger is gone, and the pipeline persists for the life of the service.
