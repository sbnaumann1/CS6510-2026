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

## Earlier reports

`report-20260824-*.json` and `report-20260916-185335.json` predate the backend and were produced
by `mockserver/MockServer.java` (in-process `ConcurrentHashMap`, no persistence). They are the
ceiling reference, not a comparable architecture.
