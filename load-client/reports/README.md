# Load-test reports

## Week 1 — Monolith (FastAPI + PostgreSQL), 2026-09-18

Host: Apple Silicon, 11 cores / 18 GB, macOS 15.6. PostgreSQL 17.11 native (Homebrew,
Unix socket), uvicorn 4 workers / pool 12, `--http h11`.

| Report | Scenario | Config | tx/s | items/s | COMPLETE err% | Invariant |
| --- | --- | --- | --- | --- | --- | --- |
| `report-20260918-170238.json` | **V6 — normal run (primary submission)** | 10 stations / 60 s, stock 10 000 | 328 | 7 987 | 56.76 % | PASS |
| `report-20260918-170437.json` | **V7 — clean companion** | 10 stations / 60 s, stock 2 000 000 | 788 | 8 239 | 0.00 % | PASS |
| `report-20260918-170702.json` | **V8 — stress (assignment config)** | 100 stations / 120 s, stock 10 000 | 276 | 9 296 | 68.88 % | PASS |
| `report-20260918-170858.json` | Tuning probe — 8 workers | 10 stations / 60 s, stock 2 000 000 | 785 | 8 247 | 0.00 % | — |
| `report-20260918-195637.json` | Short smoke run (README walkthrough) | 10 stations / 20 s, stock 10 000 | 508 | 7 960 | 32.87 % | PASS |
| `report-20260918-170006.json` | ⚠️ Pre-fix failed run, **not** a result | 10 stations / 60 s | 0 | 0 | — | — |

### Reading the V6 error rate

V6's 56.76 % `COMPLETE_TRANSACTION` error rate is **correct behaviour, not a fault.** The load
client's Zipf sampler gives the rank-1 SKU about 12 % of all scans, so at the spec's default
10 000 units SKU-000001 and SKU-000002 deplete partway through the run. Every later basket
containing a depleted SKU must fail with `409 INSUFFICIENT_STOCK` rather than oversell
(FR-004). V7 is the same code and the same workload with enough stock that nothing depletes —
it returns **0.00 % errors at 788 tx/s**, which is what isolates depletion from defect.

The graded check is the invariant, not the error rate: for every SKU,
`initial_stock − current_stock == completed line items`, and stock never negative. It passes on
every run above, including the 100-station stress run (225 431 units reconciled exactly).

See `backend/specs/001-checkout-backend/research.md` R13 for the depletion arithmetic.

### The excluded report

`report-20260918-170006.json` recorded 0 transactions and a 100 % `START_TRANSACTION` error
rate. That was uvicorn's httptools parser silently dropping the request body on the
`Upgrade: h2c` header that `java.net.http.HttpClient` sends by default — a server bug, fixed by
switching to `--http h11`. Kept only as provenance for research R14; it is not a measurement of
the architecture.

## Week 2 — Layered architecture, 2026-09-24

Same host and server config as Week 1 (4 workers / pool 12, `--http h11`). Code: the four-layer
refactor (`backend/specs/002-layered-architecture/`). Produced by
`backend/scripts/run_load_tests.sh all` with `STOCK_PER_ITEM=2000000`, so nothing depletes.

| Report | Scenario | Config | tx/s | items/s | COMPLETE err% | Invariant |
| --- | --- | --- | --- | --- | --- | --- |
| `report-20260924-215706.json` | **V7-style clean normal run** | 10 stations / 60 s, stock 2 000 000 | 638 | 6 707 | 0.00 % | PASS |
| `report-20260924-215909.json` | **Clean stress run** | 100 stations / 120 s, stock 2 000 000 | 832 | 8 743 | 0.00 % | PASS |

Every attempted transaction completed in both runs: 38 270 / 38 270 normal, 99 985 / 99 985
stress.

Two things to keep in mind when comparing these numbers:

- **The normal run's throughput is host noise, not the refactor.** A same-session A/B on
  2026-09-23 measured the pre-refactor code at 741 tx/s and the layered code at 733 tx/s under
  this workload (`backend/specs/002-layered-architecture/reports/`). This run's 638 tx/s has a
  lower p50 (0.60 ms START) but a higher p95 than that A/B. That pattern points to other load on
  the host.
- **The stress run's `COMPLETE` tail (p95 143 ms, p99 235 ms) is not comparable to Week 1's
  V8.** V8 used default stock, so about 69 % of completions failed fast with a 409 once
  SKU-000001 ran out. Here every completion succeeds and has to take the row lock on the hot
  SKUs, so 100 stations queue behind each other. Compare it against a Week 1 stress run at
  2 000 000 stock if one is made, not against V8.

## Week 5 — Domain services under Docker Compose, 2026-10-13

These are the graded reports for spec 005, in [`005-service-architecture/`](./005-service-architecture/).
Four domain services (Catalog, Checkout, Inventory, Analytics) run behind an nginx gateway on
`:8080`, all over one PostgreSQL 17 container.

**Setup**
- Host: Apple Silicon, 11 cores / 18 GB.
- Docker Desktop: Docker 29.8.2, Compose v5.6.0, with 11 CPUs and 13.1 GiB allocated (at least the 8 CPU / 8 GB the plan requires).
- Services (WORKERS / DB_POOL_SIZE):

  | Service | WORKERS | DB_POOL_SIZE |
  | --- | --- | --- |
  | checkout | 6 | 10 |
  | catalog | 2 | 2 |
  | inventory | 1 | 4 |
  | analytics | 1 | 4 |

  Analytics runs one persistent pipeline.
- Each run used a fresh stack (`docker compose down -v`, then `up --wait`) and the assignment's exact client flags.
- Produced by `backend/scripts/run_load_tests.sh all`.

| Report | Scenario | Config | tx/s | items/s | COMPLETE err% | Invariant |
| --- | --- | --- | --- | --- | --- | --- |
| `005-service-architecture/report-20261013-093301.json` | **Default parameters** (`./run.sh`) | 10 stations / 60 s, stock 10 000 | 220.6 | 4 250 | 45.4 % | PASS |
| `005-service-architecture/report-20261013-093519.json` | **Stress** (`--stations=100 --duration=120`) | 100 stations / 120 s, stock 10 000 | 190.9 | 5 242 | 61.7 % | PASS |
| *Week 4 baseline* `report-20261002-163856.json` | default, native processes | 10 stations / 60 s | 320.9 | 7 562 | — | PASS |
| *Week 4 baseline* `report-20261002-164059.json` | stress, native processes | 100 stations / 120 s | 269.1 | 9 078 | — | PASS |

**Errors.** Every error in both runs is a contract-defined `409 INSUFFICIENT_STOCK` at COMPLETE:
47 959 of them, once the hot SKUs sell out. That is the same behaviour as in V6/V8 above. There
were no `5xx` responses, timeouts or tracebacks.

**Throughput is 69 % (default) and 71 % (stress) of Week 4.** The architecture adds hops,
and the client is closed-loop: each station waits for each response. So any fixed per-request
cost lowers transactions per second directly. Measured mean latency per scan:
- Week 4, native: 0.98 ms.
- Week 5: 1.83 ms. That is about +0.85 ms per request, on every request.

A diagnostic A/B (30 s each, 10 stations, stock 200 000 so nothing depletes; not committed)
splits it up:

| Path | tx/s | SCAN mean |
| --- | --- | --- |
| Host → Docker Desktop port forwarding → gateway | 420.6 | 1.74 ms |
| Load client inside the compose network → gateway | 470.5 | 1.54 ms |

At 10 stations that A/B puts about 0.2 ms on port forwarding. The spec 006 investigation
measured the gap at 100 stations, in one session, and found a different picture:

- **The 005 code itself is not slower.** Natively, in one process, it serves the stress
  workload at 827 tx/s against the Week 4 monolith's 818.
- **Docker Desktop's host→VM port forwarding is ≈ 44 % of the gap.** Running the client
  inside the compose network took tx/s from 409 to 584. At 100 stations the forwarding
  proxy is itself contended.
- **VM overhead on the services and PostgreSQL is ≈ 52 %.** That is syscalls, the virtual
  network and the extra container hop: a checkout request costs about 2× the CPU it costs
  natively.
- **Two suspects turned out not to matter.** PostgreSQL over TCP instead of the Unix socket
  is ≈ 4 %, which is noise, so 005 R8 is not a lever. nginx's own processing is ≈ 0.

Why a small per-hop cost turned into a 30–45 % loss: 68 % of baskets contain SKU-000001, and
COMPLETE held that row lock across 3–4 client round trips. Completions serialize on it, so
throughput is inversely proportional to round-trip latency, and the closed-loop client ties
scan throughput to it. The stress run's COMPLETE tail (p95 225 ms vs 64 ms in Week 4) is that
queue.

The full measurements are in
[`backend/specs/006-service-perf-investigation/investigation.md`](../../backend/specs/006-service-perf-investigation/investigation.md).
Spec 006 then shortened the lock-holding critical section to one statement; see Week 6 below.

## Week 6 — Spec 006 hot-path latency, 2026-10-16

These are the reports in [`006-hot-path-latency/`](./006-hot-path-latency/). The stack,
services and configuration are the same as Week 5, with three code changes:
- COMPLETE's critical section is one statement, so the hot-SKU locks wait on `COMMIT` only.
- Start and scan run in autocommit, one round trip each.
- The services use uvicorn's `httptools` parser behind the gateway.

They were produced by `REPORT_DIR=006-hot-path-latency backend/scripts/run_load_tests.sh all`,
on a fresh stack for each run, with the assignment's exact client flags. The host was on
battery power, so compare these with each other and with the same-session numbers in the
spec 006 investigation rather than with earlier weeks' absolute values.

| Report | Scenario | Config | tx/s | items/s | COMPLETE err % | COMPLETE p95 | Invariant |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `006-hot-path-latency/report-20261016-082652.json` | **Default parameters** | 10 stations / 60 s, stock 10 000 | 262.5 | 5 615 | 50.9 % | 4.8 ms | PASS |
| `006-hot-path-latency/report-20261016-082910.json` | **Stress** | 100 stations / 120 s, stock 10 000 | 224.5 | 6 804 | 65.4 % | 121.6 ms | PASS |
| *Week 5* `005-service-architecture/report-20261013-093301.json` | default | 10 stations / 60 s | 220.6 | 4 250 | 45.4 % | 6.0 ms | PASS |
| *Week 5* `005-service-architecture/report-20261013-093519.json` | stress | 100 stations / 120 s | 190.9 | 5 242 | 61.7 % | 225.0 ms | PASS |

Throughput is +19 % (default) and +18 % (stress) tx/s, and +32 % / +30 % items/s. Every error
is a contract-defined `409 INSUFFICIENT_STOCK`. The higher error percentage is a consequence
of the higher throughput: the hot SKUs sell out sooner within the fixed run time. Same-session
before/after runs, with ample stock, are in the investigation's
[Outcome](../../backend/specs/006-service-perf-investigation/investigation.md#outcome-spec-006-implementation).

## Earlier reports

`report-20260824-*.json` and `report-20260916-185335.json` predate the backend and were produced
by `mockserver/MockServer.java` (in-process `ConcurrentHashMap`, no persistence). They are the
ceiling reference, not a comparable architecture.
