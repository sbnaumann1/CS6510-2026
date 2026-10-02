# Quickstart: Pipelined Analytics Architecture

**Phase 1 Output**: Validation scenarios demonstrating feature end-to-end

**Date**: 2026-10-02

---

## Scenario 1: Verify Pipeline Produces Correct Rankings

**Goal**: Confirm that the pipelined analytics produces the same popular items ranking as the previous synchronous implementation.

**Prerequisites**:
- Backend server running (`backend/run.sh`)
- Fresh database with catalog seeded (`backend/scripts/seed.py --reset`)
- Load client built (`load-client/build.sh`)

**Setup**:

```bash
# Terminal 1: Start backend
cd backend && ./run.sh

# Terminal 2: Seed fresh database (if not already done by run.sh)
cd backend && uv run python scripts/seed.py --reset

# Terminal 3: Wait for server to be ready, then run load client with default settings
sleep 10
cd load-client && ./run.sh
```

**Expected Outcomes**:
- Load client completes successfully
- JSON report generated (e.g., `reports/report-20260930-123456.json`)
- Report includes `"popularItems"` array with ranked items (top 10 shown by default)
- Example output:
  ```json
  {
    "popularItems": [
      {"rank": 1, "sku": "SKU-000001", "name": "Item 1", "scanCount": 132},
      {"rank": 2, "sku": "SKU-000002", "name": "Item 2", "scanCount": 65},
      ...
    ]
  }
  ```

**Validation**:
- Items are ranked in descending order of scanCount
- Top item has highest scan count
- Each item has consistent metadata (sku, name, rank)
- Low-stock alerts are also populated (no regression)

**Command to verify**:
```bash
# Check that the report has popular items
jq '.popularItems | length' load-client/reports/report-*.json  # Should be 10 (or less if fewer items scanned)
jq '.popularItems[0].scanCount >= .popularItems[1].scanCount' load-client/reports/report-*.json  # Should be true
```

---

## Scenario 2: Verify Concurrent Filter Execution Under Load

**Goal**: Confirm that the pipeline handles high concurrent load (100 stations) without deadlock, corruption, or timing out.

**Prerequisites**:
- Backend server running
- Fresh database seeded
- Load client built

**Setup**:

```bash
# Terminal 1: Start backend
cd backend && ./run.sh

# Terminal 2: Run stress test with 100 stations for 120 seconds
cd load-client && ./run.sh --stations=100 --duration=120
```

**Expected Outcomes**:
- Stress test completes successfully (reports "Duration elapsed" and generates report)
- JSON report generated with stress test results
- Report shows 32000+ total transactions
- Popular items ranking included (with items reflecting high-concurrency scanning)
- No database errors, deadlocks, or timeouts observed in backend logs

**Validation**:
```bash
# Check transaction count is significant
jq '.totalTransactions' load-client/reports/report-*.json  # Should be >= 30000

# Verify popular items exist
jq '.popularItems | length' load-client/reports/report-*.json  # Should be >= 1

# Check error rate for transactions is reasonable (some stock outs are expected)
jq '.operations[] | select(.operation == "COMPLETE_TRANSACTION") | .errorRate' load-client/reports/report-*.json
```

**Backend Verification** (optional):
Check backend logs for pipeline execution messages:
```bash
# In backend logs, should see asyncio gather running all filters concurrently
# No explicit logging output expected (background task), but no errors should appear
```

---

## Scenario 3: Verify Analytics Off Critical Path

**Goal**: Confirm that analytics recompute does not block request processing.

**Prerequisites**:
- Backend server running
- Load client built

**Setup**:

```bash
# Terminal 1: Start backend
cd backend && ./run.sh

# Terminal 2: Monitor individual request latencies while running load
cd load-client && ./run.sh --stations=10 --duration=60 --verbose
```

**Expected Outcomes**:
- All request operations complete successfully (START_TRANSACTION, SCAN_ITEM, COMPLETE_TRANSACTION)
- Operation latencies remain consistent throughout the test
- No spikes in p95 or p99 latencies during analytics recompute windows

**Validation**:
```bash
# Check p95/p99 latencies for operations
jq '.operations[] | {operation, p95Ms, p99Ms}' load-client/reports/report-*.json

# Verify no timeouts
jq '.operations[] | select(.operation == "START_TRANSACTION") | .errorRate' load-client/reports/report-*.json  # Should be 0.0
jq '.operations[] | select(.operation == "SCAN_ITEM") | .errorRate' load-client/reports/report-*.json  # Should be 0.0
```

---

## Scenario 4: Integration Test - Filter Independence

**Goal**: Verify that individual filters can be tested in isolation.

**Prerequisites**:
- Development environment with pytest
- Python 3.11+

**Setup** (from backend directory):

```bash
# Run integration tests
uv run pytest tests/integration/test_analytics_pipeline.py -v

# Run unit tests for individual filters
uv run pytest tests/unit/test_pipeline_filters.py -v
```

**Expected Outcomes**:
- All tests pass
- IngestFilter correctly reads window counts from database
- AggregationFilter correctly buffers and forwards data
- RankingFilter correctly ranks items by scan count
- OutputFilter correctly persists snapshot to database
- Pipeline end-to-end test produces consistent results

**Test Coverage**:
```bash
# Generate coverage report
uv run pytest tests/ --cov=app.analytics --cov-report=term-missing
```

---

## Scenario 5: Verify Backward Compatibility

**Goal**: Confirm that existing API consumers of popular items ranking see no changes in behavior or output format.

**Prerequisites**:
- Backend server running with pipelined implementation
- Fresh database seeded
- Load has been generated (from any scenario above)

**Setup**:

```bash
# Query the popular items API
curl -s http://localhost:8080/analytics/popular-items | jq '.'
```

**Expected Outcomes**:
- Response has same structure as before (JSON with items array, window metadata, etc.)
- Response includes:
  ```json
  {
    "windowSize": 1000,
    "slideInterval": 500,
    "windowStart": 8000,
    "windowEnd": 9000,
    "computedAt": "2026-10-02T...",
    "items": [
      {"rank": 1, "sku": "SKU-000001", "name": "Item 1", "scanCount": 132},
      ...
    ]
  }
  ```
- Items are ranked correctly
- Names are populated from catalog cache (not just SKU codes)

**API Validation**:
```bash
# Verify response structure
curl -s http://localhost:8080/analytics/popular-items | jq 'keys' 
# Should include: windowSize, slideInterval, windowStart, windowEnd, computedAt, items

# Verify items have required fields
curl -s http://localhost:8080/analytics/popular-items | jq '.items[0] | keys'
# Should include: rank, sku, name, scanCount
```

---

## Troubleshooting

| Issue | Diagnosis | Resolution |
|-------|-----------|-----------|
| No popular items in report | Window has no scans | Run longer stress test to generate scans |
| Pipeline timeout | Database slow or locked | Check PostgreSQL performance; verify no long-running queries |
| Inconsistent ranking | Database state corrupted | Re-seed: `uv run python scripts/seed.py --reset` |
| API returns old data | Cache not invalidated | Trigger new recompute by causing scan at slide boundary |
| Filter deadlock | Queue communication issue | Check for missing None sentinel values |

---

## Success Criteria Summary

✅ **SC-001**: Pipeline produces identical ranking as synchronous implementation (verified by Scenario 1)

✅ **SC-002**: Pipeline executes under load without deadlock/corruption (verified by Scenario 2)

✅ **SC-003**: All filters execute concurrently (verified by filter-independent tests in Scenario 4)

✅ **SC-004**: Popular items API returns correct ranking immediately (verified by Scenario 5)

✅ **SC-005**: Low-stock alerts unaffected (verified by report inclusion in Scenarios 1-2)

✅ **SC-006**: Analytics recompute off critical path (verified by Scenario 3 latency checks)
