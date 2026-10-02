# Tasks: Streaming Pipeline Implementation

**Date**: 2026-10-02

---

## Format

`[T###] [Phase] [Priority] [Blocks] Description`

- **Phase**: Setup, Foundational, US1, US2, US3, Polish
- **Priority**: P1 (MVP), P2 (Scale), P3 (Quality)
- **Blocks**: Which task(s) this task unblocks
- **Depends on**: Which previous tasks must complete first

---

## Phase 1: Setup & Prerequisites

**Purpose**: Initialize project structure and verify dependencies.

- [x] **T001** [Setup] [P1] [Blocks: T010+] Verify `collections.deque` is available in Python 3.11 standard library (for circular window buffer)

- [x] **T002** [Setup] [P1] [Blocks: T010+] Verify `asyncio.Queue` is available (for inter-filter communication)

- [x] **T003** [Setup] [P1] [Blocks: T010+] Create directory `backend/tests/unit/` if not present

- [x] **T004** [Setup] [P1] [Blocks: T010+] Create directory `backend/tests/integration/` if not present

- [x] **T005** [Setup] [P1] [Blocks: T010+] Verify PostgreSQL is running and seeded: `uv run python scripts/seed.py --reset`

**Checkpoint**: Dependencies verified, directories created, database ready. ✓ COMPLETE

---

## Phase 2: Foundational (Core Pipeline Infrastructure)

**Purpose**: Define data structures and baseline pipeline orchestrator.

**⚠️ CRITICAL**: No filter implementation can begin until this phase is complete.

- [x] **T010** [Foundational] [P1] [Depends: T001-T005] [Blocks: T020+] 
  **Define ScanData class** in `backend/app/analytics/pipeline.py`:
  - Fields: `sku: str`, `scan_id: int`
  - Made dataclass with frozen=True for queue safety ✓

- [x] **T011** [Foundational] [P1] [Depends: T001-T005] [Blocks: T020+]
  **Define WindowData class** in `backend/app/analytics/pipeline.py`:
  - Fields: `window_start: int`, `window_end: int`, `scans: list[ScanData]`, `slide_count: int = 0`
  - Made dataclass with frozen=True ✓

- [x] **T012** [Foundational] [P1] [Depends: T001-T005] [Blocks: T020+]
  **Define AggregatedData class** in `backend/app/analytics/pipeline.py`:
  - Fields: `sku: str`, `scan_count: int`
  - Made dataclass with frozen=True ✓

- [x] **T013** [Foundational] [P1] [Depends: T001-T005] [Blocks: T020+]
  **Define RankedData class** in `backend/app/analytics/pipeline.py`:
  - Fields: `window_size`, `slide_interval`, `window_start`, `window_end`, `computed_at`, `ranking`
  - Made dataclass with frozen=True ✓

- [x] **T014** [Foundational] [P1] [Depends: T001-T005] [Blocks: T020+]
  **Create TERMINATE sentinel** in `backend/app/analytics/pipeline.py`: ✓

- [x] **T015** [Foundational] [P1] [Depends: T010-T014] [Blocks: T020+]
  **Create logger** in `backend/app/analytics/pipeline.py`: ✓

- [x] **T016** [Foundational] [P1] [Depends: T010-T014] [Blocks: T020+]
  **Create constants** in `backend/app/analytics/pipeline.py`:
  - WINDOW_SIZE = 1000, SLIDE_INTERVAL = 500, RANKING_DEPTH = 200 ✓

- [x] **T017** [Foundational] [P1] [Depends: T010-T016] [Blocks: T020+]
  **Create run_streaming() orchestrator function** in `backend/app/analytics/pipeline.py`:
  - Full implementation with asyncio.gather() for concurrent filters ✓

- [x] **T018** [Foundational] [P1] [Depends: T017] [Blocks: T020+]
  **Added WindowMetadata dataclass** for window bound propagation through pipeline ✓

**Checkpoint**: Data model complete, pipeline ready for filter implementation. ✓ COMPLETE

---

## Phase 3: User Story 1 - Streaming Scan Processing (Priority: P1 🎯 MVP)

**Goal**: Build working pipeline that processes scans as events and produces correct rankings.

### 3A: IngestFilter (Polls Database for New Scans)

- [ ] **T020** [US1] [P1] [Depends: T017] [Blocks: T030]
  **Implement IngestFilter class** in `backend/app/analytics/pipeline.py`:
  - Constructor: `__init__(self, session: AsyncSession, last_scan_id: int = 0)`
  - Method: `async def run(self, output_queue: asyncio.Queue) -> None:`
  - Logic:
    1. Query database: `SELECT id, sku FROM transaction_item WHERE id > last_scan_id ORDER BY id LIMIT 1000`
    2. For each row, emit `ScanData(sku=row.sku, scan_id=row.id)`
    3. Track `last_scan_id` locally (for polling resumption)
    4. When done, emit `TERMINATE` sentinel
    5. Wrap in try/except: log error, emit TERMINATE
  - Docstring: "Polls database for new scans, emits ScanData events"

- [ ] **T021** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Create unit test for IngestFilter** in `backend/tests/unit/test_ingest_filter.py`:
  - Test: `test_ingest_filter_polls_database()`
    - Mock database session
    - Mock transaction_item rows with sku, id
    - Verify IngestFilter emits ScanData with correct fields
    - Verify TERMINATE is emitted at end
  - Test should FAIL initially (TDD)

### 3B: WindowFilter (Maintains Sliding Buffer)

- [ ] **T022** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Implement WindowFilter class** in `backend/app/analytics/pipeline.py`:
  - Constructor: `__init__(self)`
  - Method: `async def run(self, input_queue: asyncio.Queue, output_queue: asyncio.Queue) -> None:`
  - Logic:
    1. Initialize `scans_buffer = deque(maxlen=WINDOW_SIZE)` (circular buffer of 1000)
    2. Initialize `scan_count = 0`, `slides_emitted = 0`
    3. Loop:
       - Receive ScanData from input_queue
       - If TERMINATE: emit all remaining buffered data as final WindowData, then emit TERMINATE, break
       - Append ScanData to buffer (old data auto-discarded when buffer full)
       - Increment scan_count
       - If scan_count % SLIDE_INTERVAL == 0:
         - Create WindowData with current buffer, slide_count
         - Emit WindowData to output_queue
         - slides_emitted += 1
    4. Wrap in try/except: log error, emit TERMINATE
  - Docstring: "Maintains sliding 1000-scan buffer, emits WindowData every 500 scans"

- [ ] **T023** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Create unit test for WindowFilter** in `backend/tests/unit/test_window_filter.py`:
  - Test: `test_window_filter_emits_on_slide()`
    - Create mock input_queue with 1500 ScanData
    - Run WindowFilter
    - Verify 3 WindowData emitted (at 500, 1000, 1500 scans)
    - Verify buffer size never exceeds 1000
    - Verify TERMINATE propagated

### 3C: AggregationFilter (Counts Scans by SKU)

- [ ] **T024** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Implement AggregationFilter class** in `backend/app/analytics/pipeline.py`:
  - Constructor: `__init__(self)`
  - Method: `async def run(self, input_queue: asyncio.Queue, output_queue: asyncio.Queue) -> None:`
  - Logic:
    1. Loop:
       - Receive WindowData from input_queue
       - If TERMINATE: emit TERMINATE, break
       - Count scans by SKU: `sku_counts = Counter(scan.sku for scan in window_data.scans)`
       - For each (sku, count), emit `AggregatedData(sku=sku, scan_count=count)`
    2. Wrap in try/except: log error, emit TERMINATE
  - Docstring: "Receives window, aggregates scan counts by SKU, emits AggregatedData"

- [ ] **T025** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Create unit test for AggregationFilter** in `backend/tests/unit/test_aggregation_filter.py`:
  - Test: `test_aggregation_filter_counts_correctly()`
    - Create mock WindowData with scans for 3 SKUs (e.g., 10 scans SKU-1, 5 scans SKU-2, 3 scans SKU-3)
    - Verify AggregationFilter emits 3 AggregatedData with correct counts

### 3D: RankingFilter (Ranks by Scan Count)

- [ ] **T026** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Implement RankingFilter class** in `backend/app/analytics/pipeline.py`:
  - Constructor: `__init__(self)`
  - Method: `async def run(self, input_queue: asyncio.Queue, output_queue: asyncio.Queue) -> None:`
  - Logic:
    1. Initialize `ranking_list = []`, `window_metadata = None`
    2. Loop:
       - Receive AggregatedData from input_queue
       - If TERMINATE: 
         - Sort ranking_list by scan_count descending, take top RANKING_DEPTH (200)
         - Create RankedData with metadata + ranking
         - Emit RankedData
         - Emit TERMINATE
         - break
       - Append AggregatedData to ranking_list
       - Save window metadata from first item (if first)
    3. Wrap in try/except: log error, emit TERMINATE
  - Docstring: "Collects AggregatedData, ranks by scan count, emits RankedData"

- [ ] **T027** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Create unit test for RankingFilter** in `backend/tests/unit/test_ranking_filter.py`:
  - Test: `test_ranking_filter_sorts_correctly()`
    - Create mock AggregatedData with 5 items (counts: 10, 3, 20, 5, 2)
    - Verify RankingFilter emits RankedData with items sorted 20, 10, 5, 3, 2

### 3E: OutputFilter (Persists to Database)

- [ ] **T028** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Implement OutputFilter class** in `backend/app/analytics/pipeline.py`:
  - Constructor: `__init__(self, session: AsyncSession)`
  - Method: `async def run(self, input_queue: asyncio.Queue) -> None:`
  - Logic:
    1. Loop:
       - Receive RankedData from input_queue
       - If TERMINATE: break
       - Convert ranking to JSON: `ranking_json = orjson.dumps(data.ranking).decode()`
       - Call repo function: `await analytics_repo.upsert_snapshot(session, data.window_size, data.slide_interval, data.window_start, data.window_end, ranking_json, data.computed_at)`
    2. After loop, commit: `await session.commit()`
    3. Wrap in try/except: log error (don't re-raise; off critical path)
  - Docstring: "Receives RankedData, persists to database, commits transaction"

- [ ] **T029** [US1] [P1] [Depends: T010-T016] [Blocks: T030]
  **Create unit test for OutputFilter** in `backend/tests/unit/test_output_filter.py`:
  - Test: `test_output_filter_persists_snapshot()`
    - Mock database session with upsert_snapshot method
    - Create RankedData with sample ranking
    - Verify OutputFilter calls upsert_snapshot with correct args
    - Verify session.commit() is called

### 3F: Pipeline Orchestrator

- [ ] **T030** [US1] [P1] [Depends: T020-T029] [Blocks: T031-T032]
  **Implement run_streaming() function** in `backend/app/analytics/pipeline.py`:
  - Logic:
    1. Get current max scan_id from database: `last_scan_id = await analytics_repo.max_scan_seq(session)`
    2. Create three asyncio queues: `ingest_q`, `window_q`, `agg_q`, `rank_q`
    3. Instantiate all four filters with correct parameters
    4. Run concurrently: `await asyncio.gather(ingest.run(...), window.run(...), agg.run(...), rank.run(...))`
  - Docstring: "Orchestrates streaming pipeline: ingest → window → agg → rank → output"

- [ ] **T031** [US1] [P1] [Depends: T030] [Blocks: T032]
  **Create integration test for full pipeline** in `backend/tests/integration/test_pipeline_streaming.py`:
  - Test: `test_streaming_pipeline_produces_ranking()`
    - Seed database with 2000+ scans
    - Run `run_streaming(session)`
    - Verify `popular_window_snapshot` has new row
    - Verify ranking is not empty and sorted by scan_count

- [ ] **T032** [US1] [P1] [Depends: T031]
  **Update scheduler** in `backend/app/analytics/scheduler.py`:
  - Change: Replace call to `popular_items.recompute()` with `pipeline.run_streaming(session)`
  - Add import: `from app.analytics import pipeline`

- [ ] **T033** [US1] [P1] [Depends: T032] [Blocks: T040+]
  **Run all unit tests** in `backend/tests/unit/test_pipeline_*.py`:
  - Verify all filter tests pass
  - Command: `uv run pytest tests/unit/test_pipeline_*.py -v`
  - Expected: All 5 tests pass (IngestFilter, WindowFilter, AggregationFilter, RankingFilter, OutputFilter)

- [ ] **T034** [US1] [P1] [Depends: T033] [Blocks: T040+]
  **Run integration test**:
  - Command: `uv run pytest tests/integration/test_pipeline_streaming.py -v`
  - Expected: Full pipeline test passes

**Checkpoint**: User Story 1 complete — streaming pipeline produces correct rankings.

---

## Phase 4: User Story 2 - Concurrency & Load (Priority: P2)

**Goal**: Verify pipeline handles stress load (100 stations, 120 seconds) without deadlock.

- [ ] **T040** [US2] [P2] [Depends: T034] [Blocks: T045]
  **Add logging to filters** in `backend/app/analytics/pipeline.py`:
  - IngestFilter.run(): Log "Ingesting scans" at start, "Scans ingested: X" at end
  - WindowFilter.run(): Log "Emitting window slide" every time
  - RankingFilter.run(): Log "Ranking complete: top 5 = [SKU, SKU, ...]"
  - OutputFilter.run(): Log "Snapshot persisted" at end

- [ ] **T041** [US2] [P2] [Depends: T034] [Blocks: T045]
  **Create stress test** in `backend/tests/integration/test_pipeline_stress.py`:
  - Test: `test_pipeline_under_high_load()`
    - Simulate 100 concurrent scan ingestion (mock)
    - Run pipeline 5 times in rapid succession
    - Verify no deadlock, all complete successfully
    - Time execution: should be < 1 second per run

- [ ] **T042** [US2] [P2] [Depends: T034] [Blocks: T045]
  **Run load client stress test**:
  - Command: `./run.sh & cd ../load-client && ./run.sh --stations=100 --duration=120`
  - Expected: Test completes, JSON report with popular items
  - Verify: `jq '.totalTransactions' load-client/reports/report-*.json` ≥ 30000

- [ ] **T043** [US2] [P2] [Depends: T042] [Blocks: T045]
  **Verify database invariant**:
  - Command: `uv run python scripts/verify_invariant.py`
  - Expected: PASS (units decremented matches transactions)

- [ ] **T044** [US2] [P2] [Depends: T034] [Blocks: T045]
  **Check for deadlocks in backend logs**:
  - Command: `grep -i deadlock backend.log || echo "No deadlocks found"`
  - Expected: No deadlocks

- [ ] **T045** [US2] [P2] [Depends: T040-T044]
  **Queue depth monitoring** (optional):
  - Add logging to IngestFilter, WindowFilter, etc. to log queue sizes
  - Verify queues don't accumulate unboundedly

**Checkpoint**: User Story 2 complete — pipeline scales without deadlock.

---

## Phase 5: User Story 3 - Filter Independence (Priority: P3)

**Goal**: Verify filters are independently testable and modular.

- [ ] **T050** [US3] [P3] [Depends: T034]
  **Create isolated IngestFilter unit test**:
  - File: `backend/tests/unit/test_filter_independence.py::test_ingest_filter_isolated`
  - Mock database, verify filter works without WindowFilter

- [ ] **T051** [US3] [P3] [Depends: T034]
  **Create isolated WindowFilter unit test**:
  - File: `backend/tests/unit/test_filter_independence.py::test_window_filter_isolated`
  - Mock input/output queues, verify filter works without AggregationFilter

- [ ] **T052** [US3] [P3] [Depends: T034]
  **Create isolated AggregationFilter unit test**:
  - File: `backend/tests/unit/test_filter_independence.py::test_aggregation_filter_isolated`
  - Mock queues, verify filter works independently

- [ ] **T053** [US3] [P3] [Depends: T034]
  **Create isolated RankingFilter unit test**:
  - File: `backend/tests/unit/test_filter_independence.py::test_ranking_filter_isolated`
  - Mock queues, verify filter works independently

- [ ] **T054** [US3] [P3] [Depends: T034]
  **Create isolated OutputFilter unit test**:
  - File: `backend/tests/unit/test_filter_independence.py::test_output_filter_isolated`
  - Mock database, verify filter works independently

- [ ] **T055** [US3] [P3] [Depends: T050-T054]
  **Run all isolation tests**:
  - Command: `uv run pytest tests/unit/test_filter_independence.py -v`
  - Expected: All 5 tests pass

- [ ] **T056** [US3] [P3] [Depends: T034]
  **Verify layer boundaries**:
  - Grep for SQL in pipeline.py: `grep -i "select\|insert\|update\|delete" backend/app/analytics/pipeline.py`
  - Expected: No SQL statements (all via analytics_repo)
  - Verify: All database calls use `analytics_repo.*` functions

- [ ] **T057** [US3] [P3] [Depends: T055-T056]
  **Document filter responsibilities** in pipeline.py docstrings:
  - Each class has clear docstring: "Does X, receives Y from queue, emits Z to queue"

**Checkpoint**: User Story 3 complete — all filters are independently testable.

---

## Phase 6: Polish & Documentation

**Purpose**: Final validation and documentation updates.

- [ ] **T060** [Polish] [P3] [Depends: T045]
  **Run full test suite**:
  - Command: `uv run pytest -q`
  - Expected: All tests pass (unit, integration, contract)

- [ ] **T061** [Polish] [P3] [Depends: T060]
  **Run contract tests**:
  - Command: `uv run pytest -q tests/contract`
  - Expected: No regressions

- [ ] **T062** [Polish] [P3] [Depends: T045]
  **Update backend/README.md**:
  - Section "How it works": Update popular items row to mention streaming pipeline
  - Add link to specs/004-streaming-analytics/
  - Update description from "batch query" to "streaming pipeline"

- [ ] **T063** [Polish] [P3] [Depends: T062]
  **Update DEVELOPMENT_HISTORY.md**:
  - Add Phase 4 section: "Streaming Pipeline Refactor"
  - Document: Spec 004 replaces Spec 003's batch approach with streaming
  - Include: Rationale, architecture change, testing results

- [ ] **T064** [Polish] [P3] [Depends: T060-T062]
  **Final load test validation**:
  - Default test: `./run.sh & cd ../load-client && ./run.sh --stations=10 --duration=60`
  - Verify: JSON report includes popular items
  - Verify: Database invariant passes

- [ ] **T065** [Polish] [P3] [Depends: T064]
  **Verify API response structure** unchanged:
  - Command: `curl -s http://localhost:8080/analytics/popular-items | jq '.'`
  - Expected: Same structure as before (windowSize, slideInterval, items[], etc.)

- [ ] **T066** [Polish] [P3] [Depends: T060-T065]
  **Code review checklist**:
  - Asyncio usage correct (async/await, no blocking)
  - Error handling non-blocking (log, don't crash)
  - Queue communication clean (TERMINATE sentinel pattern)
  - No SQL in pipeline.py
  - Docstrings complete and accurate

**Checkpoint**: Phase 6 complete — all validation done, documentation updated.

---

## Dependency Graph

```
Phase 1 (Setup) T001-T005
    ↓
Phase 2 (Foundational) T010-T017
    ↓
Phase 3 (US1) T020-T034
    ├─→ T020 (IngestFilter)
    ├─→ T022 (WindowFilter)
    ├─→ T024 (AggregationFilter)
    ├─→ T026 (RankingFilter)
    ├─→ T028 (OutputFilter)
    ↓
Phase 4 (US2) T040-T045 (dependent on US1 complete)
    ↓
Phase 5 (US3) T050-T057 (dependent on US1, can be parallel with US2)
    ↓
Phase 6 (Polish) T060-T066 (dependent on all phases)
```

---

## Execution Strategy

### MVP (Phase 1-3: Streaming Pipeline Working)

**Estimated time**: 4-6 hours

1. T001-T005: Setup (30 min)
2. T010-T017: Foundational (1 hour)
3. T020-T034: US1 Implementation (3-4 hours)
4. **STOP**: Run unit tests + integration test, verify MVP works
5. Merge to main if happy

### Full Implementation (Phase 1-6)

**Estimated time**: 8-10 hours

1. MVP (as above)
2. T040-T045: US2 Load Testing (1 hour)
3. T050-T057: US3 Isolation Testing (1 hour)
4. T060-T066: Polish & Documentation (1 hour)

### Parallel Opportunities

- **Within Phase 3**: T021, T023, T025, T027, T029 (unit tests) can be written in parallel
- **Between US2 and US3**: T040+ and T050+ can proceed in parallel (both depend on T034)
- **Polish tasks**: Most of T060-T066 can be parallel (except T062 which blocks T063)

---

## Success Indicators

| Indicator | Target | Verified by |
|:---|:---|:---|
| Unit tests pass | 100% | T033 |
| Integration tests pass | 100% | T034 |
| Default load test passes | Yes | T064 |
| Stress test (100 stations) | No deadlock | T042 |
| API structure unchanged | Yes | T065 |
| Database invariant | PASS | T043 |
| All filters testable independently | Yes | T055 |
| No SQL in pipeline.py | Yes | T056 |

---

## Notes

- All filter implementations must handle `TERMINATE` sentinel correctly
- All queues are unbounded `asyncio.Queue()` (no backpressure)
- Pipeline is off critical path (doesn't block requests)
- Error handling logs but doesn't crash
- Window metadata is preserved for API transparency
