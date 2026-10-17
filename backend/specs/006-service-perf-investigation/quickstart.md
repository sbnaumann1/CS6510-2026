# Quickstart: Validating Spec 006

> **Evidence branch.** The load/profiling harness (`harness/`) and the raw run results
> (`results/`) mentioned in this document are not in this branch's tree. They are kept on
> branch `worktree-perf-hot-path-latency` (commit `6987174`), under
> `backend/specs/006-service-perf-investigation/`. To use them, run
> `git restore --source=worktree-perf-hot-path-latency -- backend/specs/006-service-perf-investigation/harness backend/specs/006-service-perf-investigation/results`
> and add `.perf-runs/` (the harness's output directory) to `backend/.gitignore` and
> `backend/.dockerignore`.

Run from `backend/`. Prerequisites:

- Docker Desktop running (≥ 8 CPU / 8 GB)
- PostgreSQL 17 on the host Unix socket `/tmp`, for the in-process tests
- `uv`
- the load client built (`../load-client/build.sh`)

The harness is `specs/006-service-perf-investigation/harness/` (`H` below); see research R6.

## V1. Suite green in-process (FR-002, FR-003, FR-004, FR-006)

```sh
uv run pytest -q
```

**Expect**: everything passes, including:

- the unchanged contract tests (`tests/contract/`). No assertion is edited relative to 005,
  so the HTTP contract and error precedence are unchanged.
- the new integration tests:
  - an all-or-nothing short basket ([completion contract](./contracts/completion-statement.md))
  - autocommit scan/start visibility and pool hygiene ([data-model §2](./data-model.md))
- the gateway header assertions in `tests/unit/test_gateway_routes.py`
  ([service-runtime contract](./contracts/service-runtime.md))
- `test_service_locks.py`, with `uv.lock` refreshed for `checkout-data` 0.6.0

## V2. Suite green over the real gateway with httptools (US3, FR-005)

```sh
docker compose down -v
docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait
docker compose exec checkout sh -c 'echo $UVICORN_HTTP'      # → httptools
GATEWAY_URL=http://localhost:8080 \
TEST_DATABASE_URL=postgresql+asyncpg://checkout:checkout@127.0.0.1:5433/checkout_test \
  uv run pytest -q tests/contract
docker compose -f compose.yaml -f compose.test.yaml down -v
./scripts/check_independence.sh                               # 005 V6, unchanged expectations
```

**Expect**: all green, and every POST body arrives (no 422s from empty bodies).

## V3. Boundaries hold

```sh
uv run python scripts/check_boundaries.py
```

**Expect**: no violations. `complete_basket` touches only Checkout-owned tables plus
`inventory_stock`, the same set as the functions it replaces.

## V4. Load A/B in one session (SC-001, SC-004, SC-005)

Run both arms back to back, on the same power state, with nothing else running.

```sh
# arm A, baseline: main
git switch --detach origin/main && docker compose build -q
$H/run_exp.sh base100_main
# arm B, this branch
git switch 006-hot-path-latency && docker compose build -q
$H/run_exp.sh base100_006
```

**Expect** (`results/<run>/summary.txt` and `pg_statements.tsv`):

| Criterion | Target | Baseline (investigation) |
|---|---|---|
| SC-001 tx/s, 100 st / 30 s / ample stock | ≥ 500 | 409 |
| SC-001 `COMPLETE` p95 | ≤ 200 ms | 432 ms |
| SC-004 `BEGIN` calls in `pg_statements.tsv` | ≤ 1.1 × completions | ≈ total requests |
| SC-005 invariant / deadlocks / transport errors | PASS / 0 / 0 | PASS / 0 / 0 |

Arm A must reproduce the investigation baseline (409 ± ~5 % tx/s). Otherwise the session
isn't comparable: stop, and re-check the power state.

## V5. Graded stress config (SC-002)

```sh
STATIONS=100 DURATION=120 STOCK=10000 $H/run_exp.sh g_stress_006
```

**Expect**: `COMPLETE` p95 ≤ 100 ms (baseline 249 ms), items/s ≥ 5 800 (baseline 5 063),
invariant PASS, and errors limited to `409 INSUFFICIENT_STOCK`.

## V6. Native (SC-003)

```sh
APPDIR=$PWD APP=composite:app APPDIR_FLAG="--app-dir $H/composite" $H/native_exp.sh native100_006
```

**Expect**: `COMPLETE` p95 ≤ 40 ms (baseline 124 ms), invariant PASS.

## V7. Graded reports and docs (US4)

```sh
REPORT_DIR=006-hot-path-latency ./scripts/run_load_tests.sh all
```

This regenerates both graded reports under `load-client/reports/006-hot-path-latency/`.
`REPORT_DIR` is new; it defaults to `005-service-architecture`.
Afterwards, confirm by reading:

- `load-client/reports/README.md` (Week 5 section) matches investigation F1–F5 and links to it
- `backend/README.md` describes the completion statement and the autocommit routes
