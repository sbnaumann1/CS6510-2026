# Implementation Plan: Hot-Path Latency Reduction

> **Evidence branch.** The load/profiling harness (`harness/`) and the raw run results
> (`results/`) mentioned in this document are not in this branch's tree. They are kept on
> branch `worktree-perf-hot-path-latency` (commit `6987174`), under
> `backend/specs/006-service-perf-investigation/`. To use them, run
> `git restore --source=worktree-perf-hot-path-latency -- backend/specs/006-service-perf-investigation/harness backend/specs/006-service-perf-investigation/results`
> and add `.perf-runs/` (the harness's output directory) to `backend/.gitignore` and
> `backend/.dockerignore`.

**Branch**: `006-hot-path-latency` | **Date**: 2026-10-16 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/006-service-perf-investigation/spec.md`, and the
measurements in [investigation.md](./investigation.md).

## Summary

Four changes. Each is measured in the investigation or rests on a probe recorded in
[research.md](./research.md).

1. **US1, P1: completion critical section in one statement.**
   - `complete_transaction` keeps steps 1–2: lock the transaction row, read the basket.
   - It replaces stock lock → decrement → alerts → finalize, today four statements, with one
     data-modifying CTE, `repos.checkout.complete_basket`. That CTE locks the stock rows in
     SKU order before writing anything (R1, plan shape verified with `EXPLAIN`).
   - The hot-SKU locks are then held for one client round trip (`COMMIT`) instead of 4–5.
   - Prototype result: native +13 % tx/s, `COMPLETE` p95 124 → 30 ms; compose +31 %,
     p95 432 → 172 ms.
2. **US2, P2: autocommit for scan and start.** A new `get_autocommit_session` dependency,
   on an `AUTOCOMMIT` view of the shared engine (same pool), removes the `BEGIN`/`COMMIT`
   round trips from ~90 % of requests (R2, probe-verified, including pool hygiene).
3. **US3, P3: `httptools` behind the gateway.**
   - `UVICORN_HTTP=httptools` in every service image.
   - nginx explicitly clears `Upgrade`/`HTTP2-Settings` upstream, so the 001 R14 hazard is
     excluded by configuration, not by an nginx default (R3).
   - Measured +15 % under compose.
4. **US4, P3: correct the published explanation** of the Week 4 → Week 5 gap (R7).

No schema, HTTP contract, error catalog or load-client changes.

## Technical Context

**Language/Version**: Python 3.11 (unchanged).

**Primary Dependencies**: unchanged. FastAPI 0.104, uvicorn 0.24 (uvloop; `httptools` 0.8.0
is already in every service's `requirements.lock`), SQLAlchemy 2.0 async + asyncpg 0.29,
nginx 1.27.

**Storage**: PostgreSQL 17, schema unchanged (data-model §1).

**Testing**:

- pytest suite, in-process (146 tests green at branch point) and in gateway mode
  (`GATEWAY_URL`).
- New: an all-or-nothing completion integration test, an autocommit visibility/pool-hygiene
  integration test, and gateway header assertions.
- Load validation with the investigation harness (R6).

**Target Platform**: Linux containers on Docker Desktop for macOS (unchanged), plus a native
run for SC-003.

**Project Type**: Multi-service web backend (uv workspace monorepo, unchanged).

**Performance Goals** (spec SC-001 to SC-005):

| Criterion | Measure | Target | Baseline |
|---|---|---|---|
| SC-001 | compose tx/s | ≥ 500 | 409 |
| SC-001 | compose `COMPLETE` p95 | ≤ 200 ms | 432 ms |
| SC-002 | graded stress `COMPLETE` p95 | ≤ 100 ms | 249 ms |
| SC-002 | graded stress items/s | ≥ 5 800 | 5 063 |
| SC-003 | native `COMPLETE` p95 | ≤ 40 ms | 124 ms |
| SC-004 | `BEGIN` count | ≤ 1.1 × completions | ≈ all requests |
| SC-005 | invariant / deadlocks / errors | PASS, 0 deadlocks, 0 transport/5xx errors | — |

**Constraints**:

- Frozen HTTP contract and error precedence.
- Unmodified load client.
- Correctness invariants are non-negotiable:
  - all-or-nothing completion
  - stock never negative
  - deterministic lock order
  - alerts committed atomically with the decrement

**Scale/Scope**:

- ~6 source files changed: `repos/checkout.py`, `checkout_data/__init__.py`,
  `checkout_service/{service,api}.py`, 4 Dockerfiles, `nginx.conf`.
- 1 module removed (`checkout_service/alerts.py`).
- 3 test files added or extended.
- Harness parametrization, plus 2 READMEs.

There are no `NEEDS CLARIFICATION` markers. Every unknown was resolved by measurement or
probe in research.md R1–R8.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unratified speckit template, so the result is
**PASS (vacuous)**, as for 001–005. The default engineering gates used by 002–005 apply
instead:

| Gate | Pre-Phase 0 | Post-Phase 1 |
|---|---|---|
| **Contract-first**: the frozen OpenAPI contract and error catalog aren't changed | PASS | PASS. The `INSUFFICIENT_STOCK` message is built from the same sorted short-SKU set. No contract test assertion changes (quickstart V1). |
| **Correctness invariant preserved**: completion stays one DB transaction | PASS | PASS. Still inside one `session.begin()`; lock order and all-or-nothing are enforced in the statement ([completion contract](./contracts/completion-statement.md)). The new tests and `verify_invariant` cover it (V1, V4–V6). |
| **No direct service coupling** (005) | PASS | PASS. All changes stay inside Checkout and `checkout-data`; table ownership is unchanged; `check_boundaries.py` runs (V3). |
| **Simplicity / YAGNI** | PASS | PASS. No new infrastructure, no stored procedures (R1 alternatives), no ORM replacement (R8). Net code shrinks: 4 repo functions and `alerts.py` are removed. |
| **Measurable**: every SC has a runnable check | PASS | PASS. Quickstart V4–V6, as same-session A/B runs with a calibration arm (R6). |

## Project Structure

### Documentation (this feature)

```text
specs/006-service-perf-investigation/
├── spec.md
├── investigation.md                # measurements behind every decision (F1–F5)
├── plan.md                         # this file
├── research.md                     # Phase 0: R1–R8
├── data-model.md                   # Phase 1: transition, session modes, config, repo surface
├── quickstart.md                   # Phase 1: V1–V7
├── contracts/
│   ├── completion-statement.md     # repos.checkout.complete_basket
│   └── service-runtime.md          # UVICORN_HTTP, gateway header clearing, direct-port policy
├── harness/                        # load/profiling harness: on the evidence branch, not in this tree
├── results/                        # run results: on the evidence branch, not in this tree
└── tasks.md                        # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (`backend/`)

```text
backend/
├── packages/checkout_data/
│   ├── pyproject.toml                          # 0.5.0 → 0.6.0 (R5)
│   └── src/checkout_data/
│       ├── __init__.py                         # + autocommit_engine, AutocommitSessionLocal,
│       │                                       #   get_autocommit_session
│       └── repos/checkout.py                   # + complete_basket; − lock_stock, decrement_stock,
│                                               #   insert_alerts, complete_transaction
├── services/checkout/src/checkout_service/
│   ├── service.py                              # steps 3–6 → complete_basket
│   ├── api.py                                  # start/scan → Depends(get_autocommit_session)
│   └── alerts.py                               # removed (rule moves into the CTE)
├── services/{catalog,checkout,inventory,analytics}/Dockerfile
│                                               # ENV UVICORN_HTTP=httptools; --http ${UVICORN_HTTP}
├── gateway/nginx.conf                          # + proxy_set_header Upgrade "" / HTTP2-Settings ""
├── scripts/run_load_tests.sh                   # + REPORT_DIR (default 005-service-architecture)
├── uv.lock                                     # refreshed for checkout-data 0.6.0
├── README.md                                   # spec 006 note (R7)
└── tests/
    ├── integration/test_completion_atomic.py   # new: short basket modifies nothing; short list exact
    ├── integration/test_autocommit_sessions.py # new: visibility before commit; pool hygiene
    └── unit/test_gateway_routes.py             # + header-clearing assertions

load-client/reports/README.md                   # Week 5 explanation corrected (R7)
```

**Structure Decision**: This uses the existing 005 monorepo layout unchanged. Every edit
stays inside the Checkout service, the `checkout-data` shared library, the service images and
the gateway config. No new service, package or table.

## Phase 0 → Phase 1 traceability

| Spec requirement | Research | Design artifact |
|---|---|---|
| FR-001 deterministic lock order; ≤ 1 round trip after locks | R1, R4 | completion-statement contract; data-model §1 |
| FR-002 all-or-nothing; never negative; alerts atomic | R1 | completion-statement contract; data-model §1 invariants |
| FR-003 invariant, zero deadlocks | R1, R6 | quickstart V4–V6 |
| FR-004 scan/start single round trip | R2 | data-model §2 |
| FR-005 httptools only where safe | R3 | service-runtime contract |
| FR-006 contract and client unchanged | R1–R3 | quickstart V1–V2 |
| US4 README correction | R7 | quickstart V7 |

## Implementation order (input to /speckit-tasks)

1. **Foundation.**
   - Parametrize the harness paths (R6).
   - Add `REPORT_DIR` to `run_load_tests.sh`.
   - Capture the arm-A calibration baseline on `main` before any code change.
2. **US1.**
   - Add `complete_basket` and the all-or-nothing integration test.
   - Switch `service.complete_transaction` to it.
   - Delete the four superseded repo functions and `alerts.py`.
   - Bump `checkout-data` to 0.6.0 and run `uv lock`.
   - Suite green (V1). Load A/B (V4) and native (V6).
3. **US2.**
   - Add the autocommit engine, factory and dependency, plus their integration test.
   - Switch the start/scan routes. Suite green.
   - Re-run V4 and check SC-004.
4. **US3.**
   - Dockerfiles: `UVICORN_HTTP`.
   - nginx header clearing, plus the unit assertions.
   - Gateway-mode suite and `check_independence.sh` (V2). Re-run V4.
5. **Validation and US4.**
   - Graded stress (V5) and the graded reports (V7).
   - Update both READMEs.
   - Record the validation runs in `results/`, and append a short "outcome" section to
     investigation.md.

## Complexity Tracking

No violations to justify. The single CTE is denser than the four statements it replaces.
That density is the feature: the round trips *are* the latency (investigation F1). It is
documented by its own contract and covered by dedicated tests.
