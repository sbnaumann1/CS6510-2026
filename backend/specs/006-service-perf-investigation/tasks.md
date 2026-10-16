---

description: "Task list for spec 006: hot-path latency reduction"
---

# Tasks: Hot-Path Latency Reduction

**Input**: Design documents from `specs/006-service-perf-investigation/`.

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`,
`quickstart.md`, `investigation.md`.

**Tests**: The plan requests these tests (plan.md "Testing"; contract "Verified by").

- **US1**: an all-or-nothing completion integration test.
- **US2**: an autocommit visibility and pool-hygiene integration test.
- **US3**: gateway header assertions.

Each is written before its implementation. The existing 146 tests must stay green with **no
assertion edited**.

**Organization**: Tasks are grouped by user story. All paths are relative to `backend/`
unless they start with `load-client/`. `H` = `specs/006-service-perf-investigation/harness`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task).
- **[Story]**: The user story the task belongs to (US1–US4).

## Measurement rule (applies to every load-run task)

- **Same session and power state.** Run on the same power state as the calibration arm,
  with nothing else heavy running.
- **Use `caffeinate`.** Wrap runs in `caffeinate -i` so the Mac can't idle-sleep mid-run.
- **Discard sleep-affected runs.** If a run logs client timeouts, or `pmset -g log` shows a
  Sleep event during it, discard it and re-run (investigation, *Excluded runs*).
- **Native runs need a free port.** Before any native run, confirm `lsof -ti :8090` is empty.
- **Rebuild first.** Rebuild the images (`docker compose build -q`) before any compose run
  that follows a code change.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Make the investigation harness and the graded-report script runnable from this
checkout by anyone, and keep run output out of git.

- [X] T001 Parametrize `H/run_exp.sh`:
  - **Paths.** Replace the hard-coded `J`, `H`, `BACK` and `LC` with:
    - `H="$(cd "$(dirname "$0")" && pwd)"`
    - `BACK="${BACK:-$(cd "$H/../../.." && pwd)}"`
    - `LC="${LC:-$BACK/../load-client}"`
    - `RUNS="${PERF_RUNS:-$BACK/.perf-runs}"`
  - **Output.** Write each run to `$RUNS/$NAME` (was `$J/runs/$NAME`).
  - **nginx log.** Use `$RUNS/_nginxlog`, and `export PERF_RUNS="$RUNS"` before invoking
    compose so `gwlog.yaml` can interpolate it.
  - Keep all other behaviour identical (compose project `-p perf`, samplers, summaries).
- [X] T002 [P] Parametrize `H/native_exp.sh` the same way (`H`, `BACK`, `LC`, `RUNS`):
  - `APPDIR="${APPDIR:-$BACK}"`
  - the `docker compose -p perf -f "$BACK/compose.yaml" down -v` line uses `$BACK`
  - keep the port-busy guard and the worker kill loop
  - the summary call becomes `python3 "$H/summ.py"`
- [X] T003 [P] Make the compose overlays path-independent:
  - **Overlay mounts.** In `H/gwlog.yaml`, `H/proto.yaml`, `H/desc.yaml` and
    `H/atomic_desc.yaml`, replace absolute host paths with paths relative to `backend/` (the
    compose project directory, because `compose.yaml` is the first `-f`). For example,
    `./specs/006-service-perf-investigation/harness/proto/service.py:…:ro`.
  - **Log directory.** In `H/gwlog.yaml`, use `${PERF_RUNS}/_nginxlog:/var/log/nginx`.
  - Leave `H/instrument.yaml`, `H/pgsock.yaml`, `H/w10.yaml` and `H/httptools.yaml` as they
    are: they contain no host paths.
- [X] T004 [P] In `H/composite/composite.py`, replace the hard-coded `sys.path.insert(...)`
  with `sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4] / "tests"))`,
  importing `pathlib`. `parents[4]` is `backend/`.
- [X] T005 [P] Archive the investigation records:
  - Move `H/chain.sh`, `H/chain2.sh` and `H/chain3.sh` to `H/archive/`.
  - Add `H/archive/README.md`, one paragraph: "exact command chains used for the
    investigation runs; paths are that session's scratch paths and are kept only as a
    record; use `run_exp.sh`/`native_exp.sh` directly".
  - Also edit `H/layers_bench.py`'s docstring: its run command is `uv run python $H/layers_bench.py`.
- [X] T006 [P] Add `.perf-runs/` to `backend/.gitignore`, under a comment
  `# spec 006 harness output (PERF_RUNS default)`.
- [X] T007 [P] In `scripts/run_load_tests.sh`, replace `OUT="$REPORTS/005-service-architecture"`
  with `OUT="$REPORTS/${REPORT_DIR:-005-service-architecture}"`. Add one usage line to the
  header comment: `REPORT_DIR=006-hot-path-latency ./scripts/run_load_tests.sh all`.
- [X] T008 Write `H/README.md` (≤ 40 lines), covering:
  - what each script and overlay is
  - the env vars from T001–T002 (`BACK`, `LC`, `PERF_RUNS`, `APPDIR`, `APP`,
    `APPDIR_FLAG`, `STATIONS`, `DURATION`, `STOCK`, `CLIENT`, `EXTRA`, `GWLOG`)
  - the measurement rule above
  - three example invocations copied from `quickstart.md` V4–V6

  Then smoke-test the parametrized harness: `STATIONS=10 DURATION=10 GWLOG=1 $H/run_exp.sh smoke_006`
  from `backend/` must print a summary line with `invariant exit=0` and a non-empty nginx
  route table.

**Checkpoint**: The harness runs from this checkout with no edits, and its output lands in
`backend/.perf-runs/` (git-ignored).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Capture the same-session calibration arm *before* any application code changes.
At this point the branch's application code is byte-identical to `origin/main`
(verify: `git diff --stat origin/main -- packages services gateway compose.yaml` shows nothing).
After this phase, every later load result is compared against these numbers, not against
the investigation's.

**⚠️ CRITICAL**: Don't start US1 code until T009–T011 are recorded.

- [X] T009 Calibration, compose: `docker compose build -q && caffeinate -i $H/run_exp.sh base100_pre`
  (100 stations / 30 s / stock 2 000 000).
  - **Expect** 409 tx/s ± ~5 % and `COMPLETE` p95 ≈ 430 ms (investigation `base100`).
  - **If it is outside ±10 %,** stop and re-check the power state and host load before going on.
- [X] T010 Calibration, native, 005 code in one process:
  `APP=composite:app APPDIR_FLAG="--app-dir $PWD/$H/composite" caffeinate -i $H/native_exp.sh native100_pre`.
  Expect ≈ 810–830 tx/s and `COMPLETE` p95 ≈ 125–145 ms.
- [X] T011 Calibration, graded stress config:
  `STATIONS=100 DURATION=120 STOCK=10000 caffeinate -i $H/run_exp.sh g_stress_pre`.
  - **Expect** ≈ 186 tx/s, ≈ 5 060 items/s, `COMPLETE` p95 ≈ 249 ms, invariant PASS.
  - **Record** T009–T011's summary lines in a scratch note; T037 will copy them into
    `results/`.

**Checkpoint**: Calibration baselines are recorded for this session.

---

## Phase 3: User Story 1 — Completion critical section in one statement (Priority: P1) 🎯 MVP

**Goal**: After the stock locks are granted, `COMPLETE_TRANSACTION` waits on one client round
trip (`COMMIT`) instead of 4–5 ([completion contract](./contracts/completion-statement.md),
research R1).

**Independent Test**: Quickstart V1 is green. A compose load run (`base100_us1`) vs
`base100_pre` meets SC-001 (≥ 500 tx/s, `COMPLETE` p95 ≤ 200 ms). The native run
(`native100_us1`) meets SC-003 (`COMPLETE` p95 ≤ 40 ms). The invariant PASSes with
0 deadlocks.

### Tests for User Story 1 ⚠️ (write first)

- [X] T012 [P] [US1] Create `tests/integration/test_completion_atomic.py`, using the `client`
  and `session` fixtures. Follow `tests/integration/test_low_stock.py`'s style
  (`sqlalchemy.text`, `await session.commit()` after setup writes). Three tests:
  1. **`test_short_basket_modifies_nothing_and_lists_every_short_sku`**
     - Setup: start a transaction and scan `SKU-000001`, `SKU-000003` ×2 and `SKU-000010`.
       Set `current_stock` to 1 for `SKU-000003` and 0 for `SKU-000010`.
     - Snapshot `inventory_stock` for those three SKUs and `count(*)` of `low_stock_alert`.
     - Assert:
       - `POST …/complete` returns **409**, body `error == "INSUFFICIENT_STOCK"`, and
         `message == "Insufficient stock for SKU-000003, SKU-000010."`
       - the three stock rows are unchanged
       - the alert count is unchanged
       - `GET /transactions/{id}` still shows `status == "OPEN"`
     - Then restore both SKUs' stock to 100. A second `POST …/complete` returns **200**,
       and the stock rows drop by exactly 1, 2 and 1.
  2. **`test_complete_basket_contract`** (repository-level)
     - Inside `async with session.begin():`, insert a transaction row and lock it with
       `repo.lock_transaction`.
     - Call `repo.complete_basket(session, tx_id, ["SKU-000020", "SKU-000021"], [1, 1], 123, 50)`,
       with both SKUs' stock pre-set to 50.
     - Assert `row.ok is True`, `row.short is None` and `row.completed_at` is not None.
     - After the block commits, assert:
       - **exactly two** `low_stock_alert` rows exist for those SKUs, each with
         `current_stock == 49` and `threshold == 50`
       - the transaction has `status == 'COMPLETED'` and `total_amount_cents == 123`
     - Import: `from checkout_data.repos import checkout as repo`.
  3. **`test_complete_basket_short_returns_sorted_short_list`**
     - Same setup, with `SKU-000021` stock 0 and `SKU-000020` stock 5, and quantities `[2, 1]`.
     - Assert `row.ok is False`, `row.completed_at is None` and `row.short == ["SKU-000021"]`.
     - Then roll back.

  Before T013, test 1 already passes (current behaviour) and tests 2–3 fail on `AttributeError`.
  That is the expected red state.

### Implementation for User Story 1

- [X] T013 [US1] Add `complete_basket` to `packages/checkout_data/src/checkout_data/repos/checkout.py`,
  exactly as specified in `contracts/completion-statement.md`:
  - **Statement constant.** A module-level `_COMPLETE_BASKET = text(...)` holding the CTE
    from research R1:

    | CTE / clause | Definition |
    |---|---|
    | `v` | `SELECT * FROM unnest(CAST(:skus AS text[]), CAST(:qtys AS int[])) AS v(sku, qty)` |
    | `locked AS MATERIALIZED` | `… WHERE s.sku = ANY(CAST(:skus AS text[])) ORDER BY s.sku FOR UPDATE` |
    | `ok` | `count(*) = cardinality(CAST(:skus AS text[])) AND coalesce(bool_and(l.current_stock >= v.qty), false)` |
    | `dec` | `UPDATE … WHERE s.sku = v.sku AND (SELECT ok FROM ok) RETURNING s.sku, s.current_stock, v.qty` |
    | `alerts` | `INSERT INTO low_stock_alert (sku, current_stock, threshold) SELECT … WHERE current_stock < :thr AND :thr <= current_stock + qty` |
    | `fin` | `UPDATE transaction SET status = 'COMPLETED', completed_at = now(), total_amount_cents = :total WHERE id = :tx_id AND (SELECT ok FROM ok) RETURNING completed_at` |
    | final `SELECT` | `ok`, `completed_at`, and `short` = `array_agg(v.sku ORDER BY v.sku)` over `v LEFT JOIN locked … WHERE l.sku IS NULL OR l.current_stock < v.qty` |

    The SQL text is identical for every basket size: **no f-string interpolation**.
  - **Signature.** `async def complete_basket(session, tx_id, skus, qtys, total_cents, threshold) -> Row`,
    returning `(await session.execute(_COMPLETE_BASKET, {...})).one()`.
  - **Comment.** Above the constant, explain:
    - why it is one statement (hold time = round trips; investigation F1)
    - how lock order is guaranteed (the One-Time Filter on `ok` makes `locked` run before any
      write; research R1)
- [X] T014 [US1] In `services/checkout/src/checkout_service/service.py`, change
  `complete_transaction`:
  - **Replace steps 3–6** with:
    - `skus = [b.sku for b in basket]`, `qtys = [b.qty for b in basket]`
    - `total_cents` as today
    - `res = await repo.complete_basket(session, tx_id, skus, qtys, total_cents, settings.low_stock_threshold)`
    - if `not res.ok`, raise
      `ApiError(INSUFFICIENT_STOCK, "Insufficient stock for " + ", ".join(res.short) + ".")`
    - `completed_at = res.completed_at`
  - **Keep** steps 1–2 and step 7 (receipt) unchanged, and keep the whole thing inside
    `async with session.begin():`.
  - **Cleanup.** Remove the `emit_crossings` import, and update the module docstring and
    step comments to cite spec 006 research R1 instead of 005 "research R10".
- [X] T015 [US1] Delete the superseded code, then confirm
  `grep -rn -E "lock_stock|decrement_stock|insert_alerts|emit_crossings|repo.complete_transaction" packages services tests`
  returns nothing:
  - **In `packages/checkout_data/src/checkout_data/repos/checkout.py`:**
    - the functions `lock_stock`, `decrement_stock`, `insert_alerts` and `complete_transaction`
    - the constants `_LOCK_STOCK`, `_COMPLETE_TX` and `_INSERT_ALERT`
    - the now-unused imports (`ARRAY`, `Text`, `bindparam` are needed only if T013 uses them)
  - **The file** `services/checkout/src/checkout_service/alerts.py`.
- [X] T016 [US1] Bump `packages/checkout_data/pyproject.toml` `version` from `"0.5.0"` to `"0.6.0"`
  (research R5) and run `uv lock`. Then `uv run pytest -q tests/unit/test_service_locks.py`
  must pass, with the per-image `requirements.lock` files unchanged.
- [X] T017 [US1] Run `uv run pytest -q` (all green; T012's three tests pass) and
  `uv run python scripts/check_boundaries.py` (no violations). Fix code, never assertions,
  until both are clean.
- [X] T018 [US1] Load-validate US1 per the measurement rule:
  - `docker compose build -q && caffeinate -i $H/run_exp.sh base100_us1`, and
    `APP=composite:app APPDIR_FLAG="--app-dir $PWD/$H/composite" caffeinate -i $H/native_exp.sh native100_us1`
  - **Pass** if:
    - SC-001: tx/s ≥ 500 and `COMPLETE` p95 ≤ 200 ms
    - SC-003: native `COMPLETE` p95 ≤ 40 ms
    - invariant PASS and `deadlocks = 0` in `pg_db.tsv`
  - **If it falls short,** compare with the prototype runs (`results/p1_atomic`,
    `results/n3_native_atomic100`) before changing anything.

**Checkpoint**: US1 is functional, tested and measured. It is the MVP.

---

## Phase 4: User Story 2 — Autocommit for scan and start (Priority: P2)

**Goal**: Scan and start execute as one round trip, with no `BEGIN`/`COMMIT`
([data-model §2](./data-model.md), research R2).

**Independent Test**: Quickstart V1 is green. In `base100_us2`'s `pg_statements.tsv`, the
`BEGIN ISOLATION LEVEL READ COMMITTED` `calls` value is ≤ 1.1 × the report's
`COMPLETE_TRANSACTION` success + error count (SC-004).

### Tests for User Story 2 ⚠️ (write first)

- [X] T019 [P] [US2] Create `tests/integration/test_autocommit_sessions.py`. Import
  `engine, SessionLocal` and (new) `AutocommitSessionLocal` from `checkout_data`, and use
  `engine.connect()` as the observing peer connection. Two tests:
  1. **`test_autocommit_write_is_visible_before_commit`**
     - Insert a transaction (`INSERT INTO transaction (station_id) VALUES ('ac-probe') RETURNING id`)
       through an `AutocommitSessionLocal()` session.
     - Assert the peer sees it **before** `commit()` is called.
     - Clean up the row afterwards.
  2. **`test_pool_returns_connections_transactional`**
     - Loop 5 times:
       - do a write through an `AutocommitSessionLocal()` session
       - then a write through a `SessionLocal()` session, asserting the peer does **not**
         see it before commit
       - then roll it back, asserting it is gone
     - The rule under test (data-model §2): "Connections return to the pool with the
       default isolation level."

  Before T020 both fail on `ImportError`. That is the expected red state.

### Implementation for User Story 2

- [X] T020 [US2] In `packages/checkout_data/src/checkout_data/__init__.py`, after `SessionLocal`:
  - **Add:**
    - `autocommit_engine: AsyncEngine = engine.execution_options(isolation_level="AUTOCOMMIT")`
      (same pool)
    - `AutocommitSessionLocal = async_sessionmaker(autocommit_engine, expire_on_commit=False, autoflush=False)`
    - `async def get_autocommit_session()`, mirroring `get_session`
  - **Docstring.** Extend the module docstring with one sentence citing spec 006 research R2:
    "only for routes whose database work is one atomic statement".
- [X] T021 [US2] In `services/checkout/src/checkout_service/api.py`:
  - `start_transaction` and `scan_item` use `Depends(get_autocommit_session)`.
  - `complete_transaction` and `get_transaction` keep `Depends(get_session)`.
  - Update the import and the module docstring: one sentence on which routes are autocommit
    and why.
- [X] T022 [US2] In `services/checkout/src/checkout_service/service.py`, update the
  `start_transaction` and `scan_item` docstrings/comments. They run on autocommit sessions,
  so `session.commit()` is a harmless no-op kept for symmetry. On scan's not-open path,
  `rollback()` is a no-op too, and correct because the CTE's `WHERE status = 'OPEN'`
  modified nothing. **No logic change.**
- [X] T023 [US2] Run `uv run pytest -q` (all green, including T019 and the unchanged scan/start
  contract tests for 404/409 precedence). Then `docker compose build -q && caffeinate -i $H/run_exp.sh base100_us2`.
  **Pass**: SC-004 per the Independent Test above; tx/s and `COMPLETE` p95 no worse than
  `base100_us1` beyond noise (±5 %); invariant PASS.
  - **Result (`base100_us2`)**:
    - **Met:** SC-004 (`BEGIN`/complete 12.6 → 1.04); tx/s unchanged (560.7 vs 559.8).
    - **Missed:** `COMPLETE` p95 rose 146 → 224 ms, outside this task's ±5 % criterion and
      above SC-001's 200 ms.
    - **Cause** (`harness/cycle.py`): closed-loop redistribution, not lost capacity. The
      per-transaction cycle is unchanged (178.2 vs 177.9 ms); ~20 ms moved out of
      start + scans and into the lock queue. Postgres CPU fell 25 %.
    - **Resolution**: deferred to T036.

**Checkpoint**: US1 and US2 both work. Requests with one statement make one round trip.

---

## Phase 5: User Story 3 — httptools behind the gateway (Priority: P3)

**Goal**: The services parse HTTP with `httptools`, and the gateway guarantees by
configuration that `Upgrade: h2c` never reaches them
([service-runtime contract](./contracts/service-runtime.md), research R3).

**Independent Test**: Quickstart V2 is green: the gateway-mode contract suite and
`check_independence.sh`. `base100_us3` shows 0 transport errors and scan p95 ≤ `base100_us2`'s.

### Tests for User Story 3 ⚠️ (write first)

- [X] T024 [P] [US3] Add `test_gateway_clears_hop_by_hop_upgrade_headers` to
  `tests/unit/test_gateway_routes.py`:
  - Read `gateway/nginx.conf`, the same way the file's existing tests locate it.
  - Assert it contains each of these exactly once, in the `http` block (whitespace-normalized):
    - `proxy_set_header Connection "";`
    - `proxy_set_header Upgrade "";`
    - `proxy_set_header HTTP2-Settings "";`

  It fails before T025.

### Implementation for User Story 3

- [X] T025 [US3] In `gateway/nginx.conf`:
  - Add `proxy_set_header Upgrade "";` and `proxy_set_header HTTP2-Settings "";` right after
    `proxy_set_header Connection "";`.
  - Reword the comment above them: the client's Upgrade/HTTP2-Settings headers are cleared
    explicitly so services can run httptools (spec 006 research R3; 001 R14).
  - The route table is unchanged, so `test_nginx_and_dispatcher_route_tables_match` must still pass.
- [X] T026 [P] [US3] Make the same edit in all four service images:
  - **Files:** `services/catalog/Dockerfile`, `services/checkout/Dockerfile`,
    `services/inventory/Dockerfile`, `services/analytics/Dockerfile`.
  - **`ENV`.** Add `UVICORN_HTTP=httptools` to the existing `ENV` line.
  - **`CMD`.** Change `--http h11` to `--http ${UVICORN_HTTP}`; the shell form already
    expands variables.
  - **Comment.** Replace `--http h11: research R10.` with
    `--http ${UVICORN_HTTP} (default httptools): spec 006 research R3; set UVICORN_HTTP=h11 if a client that sends Upgrade: h2c can reach this port directly.`
- [X] T027 [US3] Add a superseded note to `specs/005-service-architecture/contracts/gateway-routes.md`,
  under the line about `Upgrade`/`HTTP2-Settings`: `> Spec 006: these headers are now cleared explicitly (proxy_set_header … ""), and services run httptools — see ../../006-service-perf-investigation/contracts/service-runtime.md.`
  Add a matching one-line note at the end of 005 `research.md` R10.
- [X] T028 [US3] Validate (quickstart V2):
  - Run `uv run pytest -q tests/unit/test_gateway_routes.py`.
  - Run the gateway-mode contract suite, exactly as in `quickstart.md` V2. Also check that
    `docker compose exec checkout sh -c 'echo $UVICORN_HTTP'` prints `httptools`.
  - Run `./scripts/check_independence.sh`.
  - Run `docker compose build -q && caffeinate -i $H/run_exp.sh base100_us3`.

  **Pass** if:
  - every step is green
  - `base100_us3` shows 0 transport errors (`grep -c 'timed out' $RUNS/base100_us3/client.log` → 0)
    and 0 5xx responses
  - scan p95 is ≤ `base100_us2`'s

**Checkpoint**: All three code stories are in. These are the final application numbers.

---

## Phase 6: User Story 4 — Correct the published explanation (Priority: P3)

**Goal**: The READMEs explain the Week 4 → Week 5 gap with the measured causes (research R7).

**Independent Test**: Quickstart V7's read-through. Every number quoted in the READMEs
appears in `investigation.md` or in this feature's `results/`.

- [X] T029 [P] [US4] In `load-client/reports/README.md`, Week 5 section, replace the paragraph
  from "**Throughput is 69 % (default) and 71 % (stress) of Week 4.**" through "None of them
  was applied to the graded runs." with a corrected explanation:
  - natively the 005 code matches the monolith (827 vs 818 tx/s)
  - Docker Desktop host→VM port forwarding is ≈ 44 % of the gap at 100 stations
  - VM overhead is ≈ 52 %
  - PG over TCP is ≈ 4 % (noise)
  - nginx is ≈ 0
  - the hot-SKU lock held across client round trips amplifies every per-hop cost

  End with a link to
  `../../backend/specs/006-service-perf-investigation/investigation.md`.
  Keep the existing tables untouched.
- [X] T030 [P] [US4] In `backend/README.md`, add a short "Spec 006: hot-path latency" subsection
  (≤ 15 lines) next to the existing architecture or performance material, covering:
  - completion steps 3–6 are now one statement (`repos.checkout.complete_basket`) and why
    (one round trip held under the hot-SKU lock)
  - start/scan use autocommit sessions
  - services run `httptools` behind the gateway (`UVICORN_HTTP`)
  - links to `investigation.md` and `plan.md`

**Checkpoint**: The docs match the measurements. US4 is done, except for the Week 6 table,
which T032 adds.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T031 Graded stress run on the final code (quickstart V5):
  `STATIONS=100 DURATION=120 STOCK=10000 caffeinate -i $H/run_exp.sh g_stress_006`.
  **Pass (SC-002)**: `COMPLETE` p95 ≤ 100 ms and items/s ≥ 5 800, invariant PASS, and the only
  `COMPLETE` errors are `409 INSUFFICIENT_STOCK` (no "timed out" or 5xx in `client.log`).
  - **Result (`g_stress_006`)**:
    - **Met:** items/s 6 690 (✅ ≥ 5 800); invariant PASS; only 409s; 0 timeouts.
    - **Missed:** `COMPLETE` p95 was 127 ms (❌ > 100 ms; it was 242 ms before).
    - **Cause:** the closed-loop shift described under T023 and in investigation.md
      *Outcome*.
- [X] T032 Regenerate the graded reports (quickstart V7):
  `REPORT_DIR=006-hot-path-latency caffeinate -i ./scripts/run_load_tests.sh all`.
  - This writes `load-client/reports/006-hot-path-latency/report-*.json`.
  - Then add a "Week 6: spec 006 hot-path latency" table to `load-client/reports/README.md`, in
    the Week 5 table's format: one row per new report, plus the Week 5 rows for comparison.
- [X] T033 [P] Confirm `run_load_tests.sh` still defaults to `005-service-architecture` when
  `REPORT_DIR` is unset, by inspection (no run needed).
- [X] T034 Re-run the native SC-003 check on the final code:
  `APP=composite:app APPDIR_FLAG="--app-dir $PWD/$H/composite" caffeinate -i $H/native_exp.sh native100_006`.
  `COMPLETE` p95 must be ≤ 40 ms.
  - **Result (`native100_006`)**: ❌ 63 ms. US1 alone (`native100_us1`) gave 25 ms. US2 then
    raised native tx/s 884 → 994 and moved queueing to the lock (see *Outcome*).
- [X] T035 Final gate:
  - `uv run pytest -q` all green: 146 original tests plus the new ones, no original
    assertion edited (verify with `git diff origin/main -- tests/contract tests/integration/test_*.py`,
    which may show only *added* files and the T024 addition)
  - `uv run python scripts/check_boundaries.py` clean
  - `git status` shows no stray `.perf-runs/` output
- [X] T036 Append an "## Outcome (spec 006 implementation)" section to `investigation.md`:
  - a before/after table: `*_pre` calibration vs `*_us1`, `*_us2`, `*_us3`, `g_stress_006`,
    `native100_006`, with tx/s, items/s, `COMPLETE` p50/p95/p99 and invariant
  - one line per SC-001…SC-005 saying met or not, with the number
- [X] T037 Copy the validation runs into `results/`, following the investigation's layout
  (`summary.txt`, `report-*.json`, `pg_statements.tsv`, `pg_db.tsv` per run), from
  `$RUNS/<run>/` to `specs/006-service-perf-investigation/results/<run>/`. The runs are
  `smoke_006`, `base100_pre`, `native100_pre`, `g_stress_pre`, `base100_us1`, `native100_us1`,
  `base100_us2`, `base100_us3`, `g_stress_006` and `native100_006`.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001–T008)**: no dependencies. T001–T007 touch different files and can run in
  parallel; T008 depends on T001–T004.
- **Foundational (T009–T011)**: depends on Setup, and **must** run before any application
  code change. It is the calibration arm.
- **US1 (T012–T018)**: depends on Foundational.
- **US2 (T019–T023)** and **US3 (T024–T028)**:
  - **Code**: independent of US1 and of each other; different files, except that US1 and US2
    both touch `service.py`, in different functions.
  - **Measurement**: cumulative. Each story's load run is compared with the previous story's,
    so run the stories in order US1 → US2 → US3.
- **US4 (T029–T030)**: depends only on the investigation, so it can run any time after Setup.
  T032's Week 6 table depends on T029.
- **Polish (T031–T037)**: depends on US1–US3 (final code) and T029.

### Within each story

The test task is written first and must fail for the stated reason. Then come the
implementation tasks in order, then the suite, then the load run.

### Parallel opportunities

- Setup: T002, T003, T004, T005, T006 and T007 in parallel with T001.
- US1: T012 (tests) in parallel with nothing else in US1 (T013–T015 are sequential in two files).
- US2: T019 in parallel with T020 (different files); T021 and T022 after T020.
- US3: T024 and T026 in parallel (test file vs Dockerfiles); T025 after T024.
- US4: T029 and T030 in parallel, and with any code story.
- **Load runs never run in parallel with each other, or with the test suite.** They share
  the host's CPU (measurement rule).

## Parallel Example: User Story 3

```text
Task: "T024 [US3] Add header-clearing assertions to tests/unit/test_gateway_routes.py"
Task: "T026 [US3] UVICORN_HTTP=httptools in services/{catalog,checkout,inventory,analytics}/Dockerfile"
# then
Task: "T025 [US3] Clear Upgrade/HTTP2-Settings in gateway/nginx.conf"
```

## Implementation Strategy

### MVP first (User Story 1 only)

1. Setup (T001–T008), then Foundational calibration (T009–T011).
2. US1 (T012–T018). **Stop and validate**: SC-001 and SC-003 met, invariant PASS. This alone
   is the largest measured gain (investigation: +31 % compose, `COMPLETE` p95 −60 %).

### Incremental delivery

- After the MVP, do US2 → measure → US3 → measure. Each is an isolated commit with its own
  load run, so its gain can be attributed.
- US4 and Polish come last, once the final numbers exist.
- Commit after each checkpoint: `006 T0xx–T0yy: <summary>`, following 005's commit style.
