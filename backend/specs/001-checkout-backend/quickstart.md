# Quickstart & Validation Guide

**Feature**: `001-checkout-backend` — how to bring the system up, prove it correct, and produce a
submittable report. Design rationale lives in [research.md](./research.md); schema details in
[data-model.md](./data-model.md); contract obligations in [contracts/](./contracts/README.md).

> Status: this describes the validation path for the implementation produced by
> `/speckit-tasks` → `/speckit-implement`. Commands referencing `app/` or `scripts/` do not run
> until those exist.

## Prerequisites

| Requirement | Check | Notes |
| --- | --- | --- |
| Python 3.11 + `uv` | `uv --version` | ✅ present on this host |
| JDK 21+ (for the load client) | `javac -version` | needed by `load-client/build.sh` |
| PostgreSQL 16/17 | `psql -V` | ❌ **not installed** — see below |
| Port 8080 free | `lsof -i :8080` | the load client defaults to it |

### Provision PostgreSQL (native, preferred — research R3)

```bash
brew install postgresql@17
brew services start postgresql@17
export PATH="$(brew --prefix postgresql@17)/bin:$PATH"
createdb checkout
psql -d checkout -c 'select version();'
```

Apply the tuning from R11 (`$(brew --prefix)/var/postgresql@17/postgresql.conf`), then restart:

```
shared_buffers = 4GB
work_mem = 32MB
max_connections = 200
synchronous_commit = off
wal_compression = on
checkpoint_timeout = 15min
max_wal_size = 8GB
```

### Fallback: Docker

Only if Homebrew is unavailable. Start Docker Desktop first (the daemon is not running on this
host), then `docker compose up -d`. Expect higher and noisier latencies on macOS — record which
mode produced a report, because mixing the two makes week-to-week numbers incomparable.

## Setup

```bash
cd backend
uv sync                                   # installs deps incl. asyncpg + orjson
cp .env.example .env                      # then edit DATABASE_URL if not using the Unix socket
uv run python scripts/seed.py --reset     # schema + 2000 items + stock 10000 each
```

`--reset` is idempotent and must be re-run before every measured load run — a run started against
depleted stock is not comparable to anything.

## Run the server

```bash
./scripts/run_server.sh                   # uvicorn, 4 workers, uvloop, access log off, :8080
```

Smoke check:

```bash
curl -s localhost:8080/items | head -c 200
curl -sS -X POST localhost:8080/transactions -H 'content-type: application/json' \
     -d '{"stationId":"station-01"}' -i | head -1      # expect: HTTP/1.1 201 Created
```

## Validation scenarios

### V1 — Automated test suite (all functional requirements)

```bash
uv run pytest -q                       # contract + integration + unit
uv run pytest -q tests/contract        # FR-012 / error catalog conformance only
```

Expected: all green. `tests/contract/` asserts the exact status codes and field names from
[contracts/README.md](./contracts/README.md), including `201` on start and the
`{error, message}` body on every failure path.

### V2 — User Story 1: a single checkout, end to end (P1)

```bash
uv run python - <<'PY'
import httpx
c = httpx.Client(base_url="http://localhost:8080")
tx = c.post("/transactions", json={"stationId":"station-01"}).json()
print(tx)                                            # status OPEN, transactionId tx-<n>
for sku in ["SKU-000001","SKU-000001","SKU-000002"]:
    print(c.post(f"/transactions/{tx['transactionId']}/items", json={"sku":sku}).json())
r = c.post(f"/transactions/{tx['transactionId']}/complete", json={}).json()
print(r)                                             # itemCount 3, lines grouped by sku
print(c.post(f"/transactions/{tx['transactionId']}/items", json={"sku":"SKU-000001"}).status_code)  # 409
PY
```

Expected: `status: "OPEN"` on start; `itemCount` climbing 1→2→3 with `runningTotal` matching the
catalog prices; a receipt whose `lines` collapse SKU-000001 to `quantity: 2` and whose
`totalAmount` equals the sum of the lines; `409` on the post-completion scan. Inventory for
SKU-000001 drops by exactly 2 (verify with V4).

### V3 — User Stories 2 & 3: alerts and analytics (P2, P3)

```bash
curl -s "localhost:8080/inventory/low-stock?threshold=100" | python3 -m json.tool | head -20
curl -s "localhost:8080/analytics/popular-items?limit=10" | python3 -m json.tool | head -30
```

Expected from low-stock: `threshold` echoing the override, `generatedAt` present, `alerts[]` in
timestamp-then-SKU order with every SKU below the threshold. From popular-items: `windowSize`
1000, `slideInterval` 500, `windowStart`/`windowEnd` bracketing at most 1000 scan sequence
numbers, `computedAt` no older than the last 500 scans, and `items[]` with contiguous `rank`
starting at 1. Before 1000 scans exist, fewer than `limit` items and `windowStart: 0` are correct.

### V4 — The graded correctness invariant (SC-003, SC-004)

```bash
uv run python scripts/verify_invariant.py
```

Expected output: `PASS` with per-SKU `initial - current == completed line items` for all 2 000
SKUs and no negative stock. **This is the check the course README names for grading**; treat any
failure as release-blocking regardless of how good the latency numbers look.

### V5 — Concurrency: no overselling under contention

```bash
uv run pytest -q tests/integration/test_concurrent_completion.py
```

Seeds one SKU with a small stock, fires many simultaneous completions at it, and asserts that
exactly `stock` units are sold, the remainder get `409 INSUFFICIENT_STOCK`, stock lands at 0, and
V4's invariant still holds. This is the test that a naive check-then-decrement implementation
fails.

### V6 — Normal load run (the submittable report)

```bash
uv run python scripts/seed.py --reset          # fresh stock, always
cd ../load-client && ./build.sh && ./run.sh --baseUrl=http://localhost:8080 --stations=10 --duration=60
cd ../backend && uv run python scripts/verify_invariant.py
```

Expected: a JSON report in `load-client/reports/report-<timestamp>.json` with per-operation
p50/p95/p99 and throughput, and V4 passing afterwards. SC-001/SC-002 ask for these numbers to be
recorded and reported for week-to-week comparison, not to clear a fixed bar; judge the run against
the plan's engineering target band (p50 ≤ 1.5 ms, p95 ≤ 4 ms, p99 ≤ 10 ms, ≥ 400 tx/s). Compare tail
latencies, not means.

Nonzero `COMPLETE_TRANSACTION` errors late in the run are expected, not a defect: the Zipf
sampler drives rank-1 SKU to ~12.2 % of all scans, so its 10 000 units deplete after roughly
82 000 scans and every later basket containing it correctly returns `409` (research R13). SC-007
admits exactly these contract-defined 4xx; what would fail it is a transport error, timeout, or 5xx.
Record the depletion point from the low-stock endpoint when explaining the report.

### V7 — Clean-report run (optional companion evidence)

```bash
STOCK_PER_ITEM=200000 uv run python scripts/seed.py --reset
cd ../load-client && ./run.sh --stations=10 --duration=60
```

Expected: 0 % error rate across all three operations, reached by seeding more stock rather than by
clamping it. Submit this alongside V6, labelled, so the error rate in V6 is clearly attributable to
correct depletion behaviour rather than a fault.

### V8 — Stress run (SC-008)

```bash
uv run python scripts/seed.py --reset
cd ../load-client && ./run.sh --stations=100 --duration=120
cd ../backend && uv run python scripts/verify_invariant.py
```

Expected: no transport errors or timeouts, no `INTERNAL_ERROR` responses, degraded but bounded
tail latency, and V4 still passing. Stock depletion 409s scale with throughput here; the
correctness invariant is the pass/fail criterion.

### V9 — Durability across restart

```bash
# with a completed transaction in hand
pkill -f 'uvicorn app.main' && ./scripts/run_server.sh
curl -s localhost:8080/transactions/tx-1            # still COMPLETED, same totals
uv run python scripts/verify_invariant.py           # still PASS
```

Expected: all transactions, stock levels and alerts survive (SC-004). This is what rules out the
in-memory shortcuts that would trade durability for latency.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Client reports `Unexpected status 422` | Validation handler not registered | contracts/error-catalog.md — override `RequestValidationError` |
| Client reports `Unexpected status 200` on start | Route missing `status_code=201` | set it on `POST /transactions` |
| `deadlock detected` in the PG log | Stock rows locked out of order | ensure the `ORDER BY sku ... FOR UPDATE` pre-lock (R10) runs before the batched `UPDATE` |
| `too many connections` | `workers × DB_POOL_SIZE > max_connections` | lower `DB_POOL_SIZE` or raise `max_connections` |
| Latency 5–10× the target band | Docker Desktop VM, or TCP instead of the Unix socket | switch to the native install / socket DSN (R3) |
| Popular items differ per request | Per-process window state crept in | all window state belongs in `popular_window_snapshot` (R6) |
| Throughput collapses over a run | Autovacuum falling behind on `transaction_item` | re-seed between runs; check `pg_stat_progress_vacuum` |

## Report submission

Per the course README, commit the timestamped JSON reports under `load-client/reports/` alongside
the code, and note for each run: which validation scenario produced it (V6/V7/V8), whether
PostgreSQL ran natively or in Docker, worker count, and the `verify_invariant.py` result.
