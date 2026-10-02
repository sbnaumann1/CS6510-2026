# Quickstart: Streaming Pipeline Validation

**Date**: 2026-10-02

---

## Overview

Five validation scenarios covering the streaming pipeline's functionality, from unit tests to full load tests. Each scenario is independently runnable and demonstrates one or more success criteria.

---

## Scenario 1: Unit Test - IngestFilter Polling

**Goal**: Verify that IngestFilter can poll the database and emit ScanData events.

**Prerequisites**:
- Development environment with pytest
- PostgreSQL seeded with some transactions
- IngestFilter implementation complete

**Setup** (from backend directory):

```bash
uv run pytest tests/unit/test_ingest_filter.py::test_ingest_filter_polls_database -v
```

**Expected Outcome**:
- Test passes: IngestFilter queries database for new scans
- Emits ScanData with correct sku and scan_id
- Emits TERMINATE sentinel when done

**Validation Command**:
```bash
uv run pytest tests/unit/test_ingest_filter.py -v  # All IngestFilter tests
```

**Success Criteria**:
- ✅ SC1: IngestFilter can be tested in isolation with mock database
- ✅ SC3: ScanData objects have correct structure

---

## Scenario 2: Unit Test - WindowFilter Sliding Window

**Goal**: Verify that WindowFilter buffers ScanData and emits WindowData every 500 scans.

**Prerequisites**:
- pytest environment
- WindowFilter implementation complete

**Setup**:

```bash
# Create test with mock IngestFilter emitting 1000 ScanData
uv run pytest tests/unit/test_window_filter.py::test_window_filter_emits_on_slide -v
```

**Expected Outcome**:
- WindowFilter buffers first 500 ScanData (no emit)
- On 500th scan, emits WindowData with 1000-scan window
- On next 500 scans, emits new WindowData
- Maintains exactly 1000 scans in buffer

**Validation**:
```bash
# Check that window has correct size
uv run pytest tests/unit/test_window_filter.py::test_window_buffer_size -v
# Output: Window buffer maintains exactly 1000 scans
```

**Success Criteria**:
- ✅ SC3: WindowFilter can be tested independently
- ✅ SC4: Sliding happens every 500 scans

---

## Scenario 3: Integration Test - Full Pipeline (Small Dataset)

**Goal**: Run the complete streaming pipeline end-to-end with a small seeded dataset.

**Prerequisites**:
- Backend server can be started
- PostgreSQL with seeded data (e.g., 10 items, 5000 scans)

**Setup**:

```bash
# Terminal 1: Seed database
uv run python scripts/seed.py --reset

# Terminal 2: Start backend
./run.sh

# Terminal 3: Run integration test
uv run pytest tests/integration/test_pipeline_streaming.py::test_full_pipeline -v
```

**Expected Outcome**:
- Pipeline runs without errors
- RankedData is produced and persisted to database
- `popular_window_snapshot` table has new row
- Ranking shows top 10-20 items by scan count

**Validation**:
```bash
# Check database for ranking
psql -d checkout -c "
  SELECT window_start, window_end, jsonb_array_length(ranking) as ranking_size
  FROM popular_window_snapshot 
  ORDER BY id DESC LIMIT 1;
"
# Output: window_start, window_end, ranking_size (should be populated)
```

**Success Criteria**:
- ✅ SC1: Pipeline produces ranking (structure matches expected format)
- ✅ SC4: Window metadata reflects progression

---

## Scenario 4: Verification - Streaming vs. Batch Ranking

**Goal**: Compare ranking from streaming pipeline (Spec 004) with batch pipeline (Spec 003) on same dataset.

**Prerequisites**:
- Both Spec 003 and Spec 004 implementations available
- Fresh seeded database

**Setup**:

```bash
# 1. Run Spec 003 pipeline, capture ranking
uv run pytest tests/integration/test_spec003_ranking.py::test_batch_pipeline_ranking -v

# 2. Clear snapshot, run Spec 004 pipeline
uv run python -c "
import asyncio
from app.analytics.pipeline import run_streaming
from app.db import get_session
session = get_session()
asyncio.run(run_streaming(session))
"

# 3. Compare rankings
python scripts/compare_rankings.py spec003_ranking.json spec004_ranking.json
```

**Expected Outcome**:
- Both pipelines produce identical ranking (same SKU order, same scan counts)
- Window metadata may differ (start/end), but item ranking is identical

**Validation**:
```bash
# Manually inspect both snapshots
jq '.ranking[0:5]' spec003_ranking.json  # Top 5 items
jq '.ranking[0:5]' spec004_ranking.json  # Should match
```

**Success Criteria**:
- ✅ SC1: Streaming ranking matches batch ranking

---

## Scenario 5: Load Test - 100 Stations, 120 Seconds

**Goal**: Verify streaming pipeline handles high concurrency (100 stations) without deadlock or queue overflow.

**Prerequisites**:
- Backend and load client both built
- PostgreSQL healthy and responsive
- Sufficient disk space for load reports (~100 MB)

**Setup**:

```bash
# Terminal 1: Start backend
cd backend && ./run.sh

# Terminal 2: Seed database
cd backend && uv run python scripts/seed.py --reset

# Terminal 3: Run stress test
cd load-client && ./run.sh --stations=100 --duration=120

# Terminal 4: After test completes, verify database
cd backend && uv run python scripts/verify_invariant.py
```

**Expected Outcome**:
- Load client completes without timeout
- JSON report generated (e.g., `reports/report-20261002-150000.json`)
- Report shows ~32,000+ total transactions
- Popular items ranking present in report
- Database invariant verified (`verify_invariant.py` passes)

**Validation**:
```bash
# Check transaction count
jq '.totalTransactions' load-client/reports/report-*.json

# Check popular items count
jq '.popularItems | length' load-client/reports/report-*.json

# Verify invariant
cd backend && uv run python scripts/verify_invariant.py
# Expected: PASS (Units decremented matches completed transactions)
```

**Backend Logs**:
```bash
# No errors in backend logs
grep -i "error" backend.log
# Expected: (no output, or only expected low-stock errors)

# Pipeline ran successfully
grep "pipeline" backend.log
# Expected: (logging if instrumented)
```

**Success Criteria**:
- ✅ SC2: Pipeline handles 100 concurrent stations
- ✅ SC5: No deadlock (test completes)
- ✅ SC6: Ranking reflects high-concurrency load (popular items visible)

---

## Scenario 6: Latency Measurement - p95/p99

**Goal**: Measure that the streaming pipeline doesn't introduce request latency spikes.

**Prerequisites**:
- Backend running with streaming pipeline
- Load client configured for latency measurement

**Setup**:

```bash
# Run smaller load (10 stations, 60 seconds) with timing
cd load-client && ./run.sh --stations=10 --duration=60 --verbose

# Extract operation latencies
jq '.operations[] | {operation, p95Ms, p99Ms, meanMs}' load-client/reports/report-*.json
```

**Expected Outcome**:
- All operations (START_TRANSACTION, SCAN_ITEM, COMPLETE_TRANSACTION) succeed
- p95 latencies remain consistent throughout test
- No spikes during pipeline slide windows

**Validation**:
```bash
# Check that latencies don't spike
jq '.operations[] | select(.operation == "COMPLETE_TRANSACTION") | {p95Ms, p99Ms}' load-client/reports/report-*.json
# Expected: p95 < 500ms, p99 < 1000ms (baseline, may vary by hardware)
```

**Success Criteria**:
- ✅ SC6: Analytics off critical path (no latency spikes)

---

## Scenario 7: API Compatibility - Popular Items Endpoint

**Goal**: Verify that the API response structure hasn't changed.

**Prerequisites**:
- Backend running with streaming pipeline
- Some load has been generated (from any scenario above)

**Setup**:

```bash
# Query the API
curl -s http://localhost:8080/analytics/popular-items | jq '.'
```

**Expected Output**:
```json
{
  "windowSize": 1000,
  "slideInterval": 500,
  "windowStart": 0,
  "windowEnd": 999,
  "computedAt": "2026-10-02T15:30:45.123456Z",
  "items": [
    {"rank": 1, "sku": "SKU-000001", "name": "Item 1", "scanCount": 132},
    {"rank": 2, "sku": "SKU-000002", "name": "Item 2", "scanCount": 65},
    ...
  ]
}
```

**Validation**:
```bash
# Check response has required fields
curl -s http://localhost:8080/analytics/popular-items | jq 'keys'
# Should include: windowSize, slideInterval, windowStart, windowEnd, computedAt, items

# Check items have required fields
curl -s http://localhost:8080/analytics/popular-items | jq '.items[0] | keys'
# Should include: rank, sku, name, scanCount
```

**Success Criteria**:
- ✅ SC4: API returns correct ranking structure
- ✅ SC5: Low-stock alerts unaffected (if included)

---

## Troubleshooting Guide

| Issue | Diagnosis | Resolution |
|:---|:---|:---|
| "Queue overflow" in logs | Downstream filter too slow | Check RankingFilter / OutputFilter performance; add logging |
| "No popular items" in report | Pipeline didn't run or emitted empty ranking | Check if TERMINATE was sent; verify window has scans |
| Pipeline hangs | Deadlock or blocking operation | Check for missing TERMINATE sentinel; ensure all filters propagate it |
| Database commit slow | Database I/O bottleneck | Consider `asyncio.to_thread()` for commit if needed |
| Ranking doesn't update | Window never slides (no 500+ scans) | Run longer load test or check if IngestFilter is polling |
| Rankings differ between Spec 003 and 004 | Data aggregation bug | Compare window contents; check scan count logic |

---

## Success Metrics Summary

| Metric | Target | Verified By |
|:---|:---|:---|
| Unit test isolation | All filters testable independently | Scenarios 1-2 |
| Integration | Full pipeline works end-to-end | Scenario 3 |
| Correctness | Spec 004 = Spec 003 ranking | Scenario 4 |
| Concurrency | 100 stations, 120s, no deadlock | Scenario 5 |
| Latency | No spikes during pipeline execution | Scenario 6 |
| API compatibility | Response structure unchanged | Scenario 7 |
| Throughput | ≥ 100 scans/second | Scenarios 5-6 |

---

## Running All Scenarios

**Full validation suite** (2-3 hours):

```bash
# 1. Unit tests (5 min)
uv run pytest tests/unit/test_pipeline_filters.py -v

# 2. Integration tests (10 min)
uv run pytest tests/integration/test_pipeline_streaming.py -v

# 3. Spec 003 vs 004 comparison (15 min)
python scripts/compare_rankings.py

# 4. Stress test (120 min)
./run.sh & 
cd ../load-client && ./run.sh --stations=100 --duration=120

# 5. Latency test (60 min)
./run.sh &
cd ../load-client && ./run.sh --stations=10 --duration=60 --verbose

# 6. Invariant check (5 min)
uv run python scripts/verify_invariant.py

# 7. API validation (2 min)
curl -s http://localhost:8080/analytics/popular-items | jq '.'
```

**Quick validation** (20 min):

```bash
# Unit + integration only
uv run pytest tests/ -v --tb=short

# API check
curl -s http://localhost:8080/analytics/popular-items | jq '.items | length'
```

