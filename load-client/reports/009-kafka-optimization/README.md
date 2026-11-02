# Week 9: Checkout optimization (2026-11-01)

Repository: https://github.com/sbnaumann1/CS6510-2026 (spec 009,
[findings](../../../backend/docs/009-optimization-findings.md)).

The two graded load-client reports, run with the **unmodified** load client and the assignment's
flags against the compose stack, after `docker compose down -v` and `up --build --wait`, each
followed by the cross-store check (`tools/verify_invariant.py`), which **passed both times**.

| Report | Command |
| --- | --- |
| [`report-20261101-173942.json`](./report-20261101-173942.json) | `./run.sh` (10 stations, 60 s) |
| [`report-20261101-174211.json`](./report-20261101-174211.json) | `./run.sh --stations=100 --duration=120` |

## Results

| | Default (10 / 60 s) | Stress (100 / 120 s) |
| --- | --- | --- |
| Completed transactions | 10 826 | 20 199 |
| **Transactions/s** | **180.3** | **168.0** |
| Items scanned/s | 2,885 | 4,161 |
| `START_TRANSACTION` / `SCAN_ITEM` errors | 0 / 0 | 0 / 0 |
| `COMPLETE_TRANSACTION` refusals (`409 INSUFFICIENT_STOCK`) | 5,840 (35.0 %) | 27,309 (57.5 %) |
| `START` / `SCAN` mean | 1.4 / 1.5 ms | 8.0 / 7.9 ms |
| `COMPLETE` mean / p50 / p95 / p99 | 19.5 / 18.7 / 30.2 / 43.0 ms | 162.1 / 153.9 / 268.9 / 343.4 ms |
| Invariant | PASS | PASS |

As in every earlier week, the hot SKUs sell out partway through a run at the default 10 000 units
each, and later baskets containing them are refused rather than oversold.

## Against Week 8, in the same session

The machine is slower today than on 27 October (the unchanged Week 8 code scores about 4 % lower
than its report), so the Week 8 code was re-run through the same script today
(`controls/008-rerun-today/`). `controls/linger2/` is this week's code with Checkout's producer
linger left at 2 ms.

| | Week 8 report | Week 8 re-run today | Week 9 (linger 2) | **Week 9** |
| --- | --- | --- | --- | --- |
| Default tx/s | 166.2 | 160.2 | 170.5 | **180.3** |
| Stress tx/s | 160.2 | 153.0 | 163.4 | **168.0** |
| Default items/s | 2,554 | 2,291 | 2,722 | **2,885** |
| Stress items/s | 3,858 | 3,467 | 3,894 | **4,161** |
| Stress `SCAN` mean | 7.3 ms | 12.4 ms | 8.5 ms | **7.9 ms** |
| Stress `COMPLETE` mean | 183.3 ms | 163.4 ms | 174.9 ms | **162.1 ms** |

Against the same-session Week 8 code: **+12.5 % tx/s at 10 stations, +9.8 % at 100**, and +26 %
and +20 % in items scanned per second. `tx/s` counts only completions that succeed, and a faster
system sells the hot SKUs out sooner (refusals rise from 53.7 % to 57.5 % under stress), so it
understates the gain.

## What changed

- **Scan fast path**: start and scan run through raw-ASGI handlers and a bare asyncpg pool instead
  of FastAPI and SQLAlchemy sessions; Checkout's CPU per request fell about 30 %.
- **Checkout's Kafka producer** no longer waits 2 ms to batch (`linger_ms=0`).
- Reply filtering by record key, and the nginx upstream keep-alive limit that evens out Checkout's
  workers.
- Inventory is unchanged. Experiments on its consumer loop and hot-SKU locking are in the
  findings and kept on branch `009-inventory-experiments`, not shipped.
