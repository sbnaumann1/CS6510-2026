# Persistent Streaming Pipeline for Popular-Items Analytics

## Overview

Since spec 005 (service-based architecture), popular-items analytics runs inside the
**Analytics Service**. It is **one persistent streaming pipeline** for the life of the
service. It is no longer a fresh pipeline run triggered every 500 scans.

Since spec 007 (microservices) the service has **no database**. The pipeline learns about new
scans by **pulling Checkout's scan log** (`GET /internal/scans?after=<id>`, a range read over
Checkout's `transaction_item`, whose `id` is the global scan sequence). The Checkout Service
never calls, or even knows about, Analytics. The window, the counts and the ranking live in
memory and are rebuilt from Checkout's scan log after a restart.

Design and rationale: `backend/specs/007-microservices-architecture/research.md` R5 and
`data-model.md` §4 (and 005's R5–R6 for the filters). Code:
`backend/services/analytics/src/analytics_service/pipeline.py` (filters), `supervisor.py`
(lifecycle), `scan_source.py` (the pull) and `scan_window.py` (the in-memory window).

```text
TailIngest ─q1─▶ WindowFilter ─q2─▶ AggregationFilter ─q3─▶ RankingFilter ─slot─▶ OutputFilter
 (pull scans)    (slide by id)      (running counts)        (top-200)              (latest-wins, publish)
```

Every filter is a long-running loop that keeps its state across windows.
`TERMINATE` flows downstream only at shutdown, never between windows.

## Filters

### 1. TailIngest (replaces the per-run IngestFilter)
- **Input**: Checkout's scan log, over HTTP. It polls `GET /internal/scans?after=<last_id>&limit=1000`, which Checkout answers with a primary-key range read (`WHERE id > :after ORDER BY id LIMIT 1000`) and the highest committed scan id.
- **Output**: `ScanData(scan_id, sku)` in id order, plus `Watermark(boundary)` once every id ≤ boundary is accounted for.
- **Gap rule**: concurrent scans commit out of id order, so the tail keeps a *contiguous* watermark `last_id`. Rows above a hole wait until the hole fills. A hole still open after 1 s (`POPULAR_GAP_GRACE_MS`) is a sequence value that never committed: the tail logs `gap skipped id=<n>` and moves on.
- **Pacing**: full batches are read back to back, so a backlog drains at DB speed. It sleeps `POPULAR_POLL_MS` (100 ms) only when caught up or waiting on a hole. If Checkout cannot be reached it keeps its position, backs off 1 s and retries. If Checkout's highest scan id falls below its position, or Checkout names a different scan log (`logId`), the log was reset and the supervisor starts over.

### 2. WindowFilter
- Keeps a deque of recent scans. The window is defined by **scan id**, not arrival count.
- On `Watermark(b)` (every multiple of 500) it emits `Slide(b−1000, b, added, evicted)`: the SKUs entering and leaving the 1000-scan window.

### 3. AggregationFilter
- Keeps **one running `Counter`** for the pipeline's lifetime. Each slide applies +1 per added scan and −1 per evicted scan, which is O(500) work per slide rather than a fresh count of 1000.
- It emits `WindowCounts`.

### 4. RankingFilter
- Sorts by `(−count, sku)`, so ties break by SKU, matching the SQL `window_counts`. It keeps the top 200 and emits `RankedData`.

### 5. OutputFilter
- Reads from a **latest-wins slot**: if several windows are ready at once, only the newest is published, because clients only ever see the latest ranking.
- **Publishes each window into memory** (nothing is persisted), where the read path serves it, and logs `window published start=<s> end=<e>`. Before the first window exists, the read path answers from the in-memory `ScanWindow` of recent scans.

## Lifecycle (supervisor.py)

1. **No leader election.** Exactly one instance runs. 005's database lock existed so that replicas would not fight over one snapshot row; with state in memory there is nothing shared to write, and a second instance would only duplicate work.
2. **Bootstrap**:
   - Read Checkout's max scan id and set `b0` to the latest 500-boundary.
   - Tail from `b0 − 1000`, so the first window published is `b0` and complete.
   - Log `pipeline started bootstrap_from=<id>`.
   - A restart therefore rebuilds from Checkout's scan log, and nothing is lost while Analytics is down. If Checkout is unreachable, bootstrap retries every second and the endpoint answers with the empty window meanwhile.
3. **Errors**: any filter exception cancels all filters, and the pipeline re-bootstraps after 1 s. A replaced scan log is not an error and starts over at once.
4. **Shutdown**: the tail is stopped, then `TERMINATE` drains downstream.

## Correctness checks

- `tests/integration/test_persistent_pipeline.py`: with 8 concurrent scanners, every published window equals a direct SQL count over the same bounds in Checkout's database, and only one pipeline instance exists for the run.
- `tests/integration/test_pipeline_restart.py`: after a restart (memory lost), the first window is the complete latest one; two pipelines over the same source compute the same windows.
- `tests/integration/test_analytics_pull.py`, `tests/failure/test_analytics_faults.py`: reading in order, rebuilding after a restart, a hole that never commits, a replaced scan log, Checkout unreachable (served from memory, then caught up), and a lost response counted once.
- `tests/unit/test_tail_ingest_gaps.py`, `tests/unit/test_pipeline_filters.py`: each filter in isolation, including late commits and permanent gaps.

## History

- **Spec 003**: a 4-stage batch pipeline, run per trigger.
- **Spec 004**: a streaming pipeline, still triggered in-process by the scan route every 500 scans and rebuilt each time.
- **Spec 005**: the trigger is gone, and the pipeline persists for the life of the service.
- **Spec 007**: the pipeline pulls Checkout's scan log instead of tailing a shared table, keeps its state in memory instead of a snapshot row, and needs no leader lock.
