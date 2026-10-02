# Tasks: Pipelined Analytics Architecture

**Input**: Design documents from `/specs/003-pipelined-analytics/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, quickstart.md

**Tests**: Integration and unit tests are included for comprehensive validation

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- **Backend**: `backend/app/analytics/`, `backend/tests/`
- Filter classes: `backend/app/analytics/pipeline.py`
- Tests: `backend/tests/integration/`, `backend/tests/unit/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and pipeline foundation

- [ ] T001 Ensure asyncio queue utilities are available in project imports
- [ ] T002 Verify that `app/db/analytics_repo.py` has required functions: `window_counts()`, `upsert_snapshot()`, `max_scan_seq()`
- [ ] T003 Create `backend/app/analytics/pipeline.py` stub module with imports for asyncio, dataclasses, logging, orjson
- [ ] T004 Create test directories: `backend/tests/integration/` and `backend/tests/unit/` (if not already present)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core pipeline infrastructure that MUST be complete before any filter implementation

**⚠️ CRITICAL**: No filter implementation can begin until this phase is complete

- [ ] T005 Define `AggregatedData` dataclass in `backend/app/analytics/pipeline.py` with fields: `sku` (str), `scan_count` (int)
- [ ] T006 Define `RankedData` dataclass in `backend/app/analytics/pipeline.py` with fields: `window_size`, `slide_interval`, `window_start`, `window_end`, `ranking` (list[dict[str, Any]])
- [ ] T007 Create `log` logger in `backend/app/analytics/pipeline.py` for error reporting
- [ ] T008 Set `RANKING_DEPTH = 200` constant in `backend/app/analytics/pipeline.py` per research.md R4
- [ ] T009 Create base test fixture in `backend/tests/integration/test_analytics_pipeline.py` that sets up test database session and mock window bounds

**Checkpoint**: Data models and constants defined - ready for filter implementation

---

## Phase 3: User Story 1 - Compute Popular Items Rankings (Priority: P1) 🎯 MVP

**Goal**: Implement a working pipeline that produces correct popular items rankings

**Independent Test**: Run the pipeline against a seeded database and verify it produces the same ranking as the synchronous implementation

### Tests for User Story 1 ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T010 [P] [US1] Create integration test `backend/tests/integration/test_analytics_pipeline.py::test_ingest_filter_reads_window_counts()` - verify IngestFilter reads from DB and emits AggregatedData
- [ ] T011 [P] [US1] Create integration test `backend/tests/integration/test_analytics_pipeline.py::test_pipeline_produces_correct_ranking()` - run full pipeline and verify ranking matches expected order
- [ ] T012 [P] [US1] Create integration test `backend/tests/integration/test_analytics_pipeline.py::test_output_filter_persists_snapshot()` - verify snapshot is upserted to DB

### Implementation for User Story 1

- [ ] T013 [US1] Implement `IngestFilter` class in `backend/app/analytics/pipeline.py` with `__init__(session, window_bounds)` and `async def run(output_queue)` method that:
  - Calls `repo.window_counts(session, window_start, window_end, RANKING_DEPTH)` to fetch scans from database
  - For each row, creates `AggregatedData(sku=row.sku, scan_count=row.scan_count)` and puts to `output_queue`
  - After all rows, puts `None` to signal end-of-stream
  - Catches exceptions, logs them, and re-raises

- [ ] T014 [US1] Implement `RankingFilter` class in `backend/app/analytics/pipeline.py` with `__init__(window_bounds)` and `async def run(input_queue, output_queue)` method that:
  - Reads all `AggregatedData` objects from `input_queue` (stopping at None sentinel)
  - Maintains list of rankings ordered by `scanCount` descending
  - Creates `RankedData` with window metadata and ranking list
  - Puts `RankedData` to `output_queue`, then puts `None`
  - Catches exceptions, logs them, and re-raises

- [ ] T015 [US1] Implement `OutputFilter` class in `backend/app/analytics/pipeline.py` with `__init__(session)` and `async def run(input_queue)` method that:
  - Reads `RankedData` from `input_queue` (stops at None sentinel)
  - Converts `ranking` list to JSON string using `orjson.dumps(data.ranking).decode()`
  - Calls `repo.upsert_snapshot(session, data.window_size, data.slide_interval, data.window_start, data.window_end, ranking_json)`
  - After receiving None, commits session with `await session.commit()`
  - Catches exceptions, logs them (never propagates as request error per research.md R6)

- [ ] T016 [US1] Implement `recompute_windowed(session)` function in `backend/app/analytics/pipeline.py` that:
  - Calls `max_seq = await repo.max_scan_seq(session)` to get current scan sequence
  - Computes `window_start = max(0, max_seq - settings.popular_window_size)` and `window_end = max_seq`
  - Creates three asyncio queues: `ingest_queue`, `aggregation_queue`, `output_queue`
  - Instantiates all four filters with appropriate configuration
  - Runs all filters concurrently via `await asyncio.gather(ingest.run(...), aggregation.run(...), ranking.run(...), output.run(...))`

- [ ] T017 [US1] Modify `backend/app/analytics/scheduler.py` to import and use `pipeline.recompute_windowed()` instead of `popular_items.recompute()` in the `recompute_window()` function

- [ ] T018 [US1] Run integration test `test_pipeline_produces_correct_ranking()` to verify ranking correctness

**Checkpoint**: At this point, User Story 1 should be fully functional - pipeline computes and persists rankings correctly

---

## Phase 4: User Story 2 - Handle High-Concurrency Analytics (Priority: P2)

**Goal**: Verify pipeline handles stress load (100 stations, 120 seconds) without deadlock or corruption

**Independent Test**: Run load client with stress parameters and verify popular items API returns correct ranking

### Tests for User Story 2 ⚠️

- [ ] T019 [P] [US2] Create integration test `backend/tests/integration/test_analytics_pipeline.py::test_pipeline_concurrent_writes()` - run pipeline while concurrent writes occur, verify no corruption
- [ ] T020 [P] [US2] Create integration test `backend/tests/integration/test_analytics_pipeline.py::test_pipeline_completes_under_load()` - time pipeline execution and verify it completes within timeout window

### Implementation for User Story 2

- [ ] T021 [US2] Add comprehensive logging to pipeline.py filters for observability:
  - Log filter start/stop in `IngestFilter`, `AggregationFilter`, `RankingFilter`, `OutputFilter`
  - Log row counts and window bounds
  - Log rankings generated with top 5 items

- [ ] T022 [US2] Run load client stress test with `--stations=100 --duration=120` and verify:
  - Pipeline completes without errors in backend logs
  - Popular items API returns ranking (via curl or load client report)
  - Load client report shows `"popularItems"` array with ranked items
  - No database deadlocks or connection errors

- [ ] T023 [US2] Verify queue communication works without deadlock:
  - All four filters receive end-of-stream None sentinel correctly
  - No infinite loops in filter loops
  - OutputFilter commits transaction successfully even under high load

**Checkpoint**: User Story 2 complete - pipeline scales to 100 concurrent stations without deadlock

---

## Phase 5: User Story 3 - Decompose Analytics Into Independent Filters (Priority: P3)

**Goal**: Verify filters are independently testable and maintainable

**Independent Test**: Unit test each filter in isolation with mock queues

### Tests for User Story 3 ⚠️

- [ ] T024 [P] [US3] Create unit test `backend/tests/unit/test_pipeline_filters.py::test_ingest_filter_isolated()` - mock database, verify IngestFilter reads and emits correctly
- [ ] T025 [P] [US3] Create unit test `backend/tests/unit/test_pipeline_filters.py::test_aggregation_filter_isolated()` - mock input queue, verify filter forwards data and propagates None
- [ ] T026 [P] [US3] Create unit test `backend/tests/unit/test_pipeline_filters.py::test_ranking_filter_isolated()` - provide test AggregatedData via mock queue, verify ranking output
- [ ] T027 [P] [US3] Create unit test `backend/tests/unit/test_pipeline_filters.py::test_output_filter_isolated()` - mock database and RankedData input, verify upsert_snapshot call

### Implementation for User Story 3

- [ ] T028 [US3] Verify `AggregationFilter` is a true buffering stage:
  - Does not depend on any specific implementation details of upstream/downstream filters
  - Can be replaced or enhanced independently (e.g., add filtering logic in future)
  - Uses standard asyncio queue operations only

- [ ] T029 [US3] Document each filter's responsibility in `backend/app/analytics/pipeline.py` docstrings:
  - IngestFilter: "Reads scan data from DB within window bounds, emits AggregatedData"
  - AggregationFilter: "Buffers and forwards aggregated scan counts between stages"
  - RankingFilter: "Ranks items by scan count, emits RankedData with window metadata"
  - OutputFilter: "Persists snapshot to database and commits transaction"

- [ ] T030 [US3] Run unit tests from T024-T027 and verify all pass, demonstrating filter independence

- [ ] T031 [US3] Verify layer boundaries are maintained:
  - No SQL in filter implementations (use analytics_repo functions)
  - No queue logic in services or scheduler
  - Clean separation between pipeline (app/analytics/) and database (app/db/)

**Checkpoint**: User Story 3 complete - all filters are independently testable and modular

---

## Phase N: Polish & Cross-Cutting Concerns

**Purpose**: Final validation and documentation

- [ ] T032 [P] Run `backend/scripts/verify_invariant.py` after a full load test to confirm transaction correctness
- [ ] T033 [P] Run complete pytest suite: `uv run pytest -q` and verify all tests pass
- [ ] T034 [P] Run contract tests: `uv run pytest -q tests/contract` and verify no regressions
- [ ] T035 Run `backend/specs/003-pipelined-analytics/quickstart.md` validation scenarios:
  - Scenario 1: Default load test produces correct ranking
  - Scenario 2: Stress test (100 stations, 120s) completes without error
  - Scenario 3: API returns correct ranking
  - Scenario 4: Individual filters can be unit tested
  - Scenario 5: Backward compatibility - existing API contract unchanged

- [ ] T036 [P] Code review checklist:
  - All filter implementations follow the pipeline.py pattern
  - Error handling logs exceptions without blocking requests
  - Queue communication uses None sentinel for end-of-stream
  - No blocking operations in async methods (async/await used correctly)

- [ ] T037 Performance profiling (optional):
  - Time full pipeline execution with different window sizes
  - Verify p95/p99 request latencies not impacted during recompute
  - Document any bottlenecks found

- [ ] T038 Documentation:
  - Update `backend/README.md` to mention pipelined analytics (replace synchronous mention)
  - Link to specs/003-pipelined-analytics/ for architecture details

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3+)**: All depend on Foundational phase completion
  - User stories can proceed in parallel (if staffed) or sequentially
  - Each story independently testable and deliverable

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational phase - MVP, no dependencies on other stories
- **User Story 2 (P2)**: Can start after US1 is implemented (load testing requires working pipeline)
- **User Story 3 (P3)**: Can start in parallel with US2 (unit testing independent of implementation)

### Within Each User Story

1. Tests written FIRST (they fail initially)
2. Implementation completed (tests now pass)
3. Integration verified
4. Story marked complete

### Parallel Opportunities

- **Setup Phase**: All [P] tasks can run in parallel (T001-T004)
- **Foundational Phase**: T005-T008 can run in parallel; T009 depends on earlier tasks
- **User Story 1**:
  - Tests T010-T012 can be written in parallel
  - Filter implementations (T013-T015) can happen in parallel if different developers
  - T016 depends on all filters being complete
  - T017-T018 complete the story
- **User Story 2**: Load tests (T019-T020) can run in parallel after US1 is complete
- **User Story 3**: Unit tests (T024-T027) can be written in parallel; implementation (T028-T031) sequential

---

## Parallel Example: Full Team Implementation

```
Team Member A: Foundational Phase (T005-T009)
Team Member B: Waits for T009 completion, starts User Story 1 (T013-T017)
Team Member C: Waits for T009 completion, starts writing User Story 1 tests (T010-T012)

After US1 complete:
Team Member B: Starts User Story 2 (T021-T023)
Team Member C: Starts User Story 3 unit tests (T024-T027)

Final: All run Polish phase tasks in parallel (T032-T038)
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T004)
2. Complete Phase 2: Foundational (T005-T009)
3. Complete Phase 3: User Story 1 (T010-T018)
4. **STOP and VALIDATE**: Run default load test, verify popular items ranking correct
5. Deploy/demo if ready (MVP complete!)

### Incremental Delivery

1. Setup + Foundational → Foundation ready
2. + User Story 1 → Test with default load → Deploy/Demo (MVP!)
3. + User Story 2 → Test with stress load → Deploy/Demo
4. + User Story 3 → Verify filter independence → Deploy/Demo
5. + Polish → Final validation → Release

### Stress Test Workflow

After US1 complete:
```bash
# Terminal 1
cd backend && ./run.sh

# Terminal 2 (in another shell)
cd backend && uv run python scripts/seed.py --reset
cd load-client && ./run.sh --stations=100 --duration=120

# Terminal 3 (verify results)
cd backend && uv run python scripts/verify_invariant.py
jq '.popularItems | length' load-client/reports/report-*.json
```

---

## Success Checklist (Per Task/Story)

### User Story 1 (MVP)
- [ ] `T018`: Default load test passes, popular items ranking correct
- [ ] Backward compatibility: existing popular items API returns same structure
- [ ] Pipeline completes within request timeout window (off critical path)

### User Story 2 (Scale)
- [ ] `T022`: Stress test (100 stations, 120s) completes successfully
- [ ] `T023`: No database deadlocks or queue blocking
- [ ] Popular items API returns correct ranking after high-concurrency run

### User Story 3 (Maintainability)
- [ ] `T030`: All unit tests pass (filters work in isolation)
- [ ] `T031`: Layer boundaries maintained, no SQL in filters

### Polish
- [ ] `T035`: All quickstart.md scenarios pass
- [ ] `T033`: Full test suite passes (no regressions)
- [ ] `T038`: Documentation updated

---

## Notes

- All filter implementations use `asyncio.Queue` per research.md R1
- Concurrency model: `asyncio.gather()` per research.md R2
- Stream termination: `None` sentinel per research.md R3
- Error handling: log and propagate per research.md R6
- Ranking depth: 200 items per research.md R4
- Database access: reuse `analytics_repo` functions per research.md R5
- Commit after each task or logical group
- Stop at any checkpoint (after Phase 1, 2, or each story) to validate independently
