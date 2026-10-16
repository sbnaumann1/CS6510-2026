# Performance Investigation: Latency in the 005 Service Architecture

**Date**: 2026-10-14 – 2026-10-16 · **Code under test**: `main` @ `7e77394` (spec 005), with
`f24177e` (last pre-005 commit, the spec 004 monolith) as the control.

**Questions**

1. Why did the 005 refactor lose ~30–45 % of throughput against Week 4 (stress run:
   269 → 191 tx/s, 9 078 → 5 242 items/s, `COMPLETE` p95 64 → 225 ms)?
2. Independent of the refactor, where does a request's latency go, and which parts of it
   come from the way the app is written?

## TL;DR

**Q1, the regression: the 005 code didn't cause it. Deploying it under Docker Desktop did.**

- Run natively in the same session, both codebases serve the stress workload at the same rate:

  | Code, deployed natively | tx/s, Oct 14 | tx/s, Oct 16 rerun |
  |---|---|---|
  | pre-005 monolith | 787 | 818 |
  | 005 services in one process | 809 | 827 |
- Under compose the same 005 code serves **409 tx/s** (calibration rerun two days later: 409.2).
- Where the native 809 → compose 409 tx/s gap goes:

  | Component | Share of gap | Evidence |
  |---|---|---|
  | Docker Desktop host→VM port forwarding | **≈ 44 %** | client inside the network: 409 → 584 tx/s |
  | VM overhead on checkout and Postgres (CPU, syscalls, virtual network) plus the extra container-to-container hop | **≈ 52 %** | per-request checkout CPU ≈ 2× native (F3) |
  | Postgres over TCP instead of a Unix socket | ≈ 4 % (noise) | 409 → 424 tx/s |
  | nginx's own processing | ≈ 0 | `$request_time − $upstream_response_time` ≈ 0.01 ms |
  | Analytics persistent pipeline | ≈ 0 | 90 ms of DB time per 30 s |
- **Why a small per-hop cost turned into a 50 % loss: the hot-SKU lock (F1).**
  - 67.7 % of baskets contain `SKU-000001`. The completion holds that row lock across 3–4
    client round trips, so completions serialize on it.
  - Throughput ≈ 0.677 / (lock hold time), so throughput is inversely proportional to
    round-trip latency.
  - Containerization made every round trip slower, and the lock converted that 1:1 into lost
    throughput. The closed-loop client then pulled scan throughput down with it.

**Q2, latency by design: two things dominate.**

1. **Under contention, the completion's critical section.** About 30 Postgres backends are
   always queued on one row lock. The lock statement averages 102 ms inside Postgres; every
   other statement takes 0.003–0.12 ms.
   - A prototype doing lock + check + decrement + alerts + finalize as **one SQL statement**
     passes all 77 contract/integration tests.
   - Natively it raises throughput **+13 %** and cuts `COMPLETE` p95 **123 → 30 ms (−76 %)**;
     lock waiters drop from ~18 to ~2.
   - Under compose: **+31 % throughput**, `COMPLETE` p95 −60 %.
   - On the graded stress config: `COMPLETE` p95 249 → **73 ms**.
2. **Without contention, framework overhead, not the database.** A scan costs **0.73 ms**
   end to end natively:
   - Postgres execution plus one round trip: **0.05 ms (7 %)**
   - the separate `BEGIN` and `COMMIT` round trips: **0.06 ms (8 %)**
   - SQLAlchemy's session layer: **0.14 ms (20 %)**
   - the HTTP stack (client, h11 parsing, FastAPI/Starlette): **~0.48 ms (65 %)**

   Under load, a checkout worker's CPU splits about 28 % SQLAlchemy, 26 % h11, 21 %
   FastAPI/Starlette and 28 % event loop/socket I/O; the app's own code is ~9 %.

## Method

- **Workload.** Unmodified Java load client (`load-client/`). Default experiment:
  100 stations, 30 s, `STOCK_PER_ITEM=2 000 000`, so nothing sells out, every completion
  takes the hot locks, and fast-failing 409s can't hide latency. The graded stress config
  (100 stations / 120 s / 10 000 stock) was re-run for the baseline and the best variants.
- **Harness** (`harness/`).
  - **`run_exp.sh`**: brings up a fresh compose project (`-p perf`) from `compose.yaml` plus
    override files:
    - `instrument.yaml` adds `pg_stat_statements`.
    - `gwlog.yaml` adds an nginx timing log (`$request_time`, `$upstream_response_time`,
      `$upstream_connect_time`).

    While the client runs it samples `pg_stat_activity` wait events at 4 Hz, plus
    `docker stats` and host CPU. Afterwards it dumps statement stats and runs
    `verify_invariant`.
  - **`native_exp.sh`**: runs either codebase natively against the host's PostgreSQL 17 over
    the Unix socket, in a dedicated `checkout_perf` database.
  - **`composite/composite.py`**: serves the four 005 service apps in one uvicorn process,
    with their lifespans, routed by the gateway's route table. That gives "005 code, Week 4
    deployment".
  - **`layers_bench.py`**: times one scan through progressively more layers on a single
    connection. **`cpubench.py` / `cpupar.sh`** compare pure-Python CPU speed natively and
    in the VM.
- **Profile.** `py-spy record` (200 Hz, 25 s) on one checkout worker under the stress load,
  from a sidecar container sharing its PID namespace. `flame_summ.py` summarizes it.
- **Prototypes.** These live in `harness/proto/`, `harness/desc/` and
  `harness/atomic_desc/`. The repository code is unchanged.
  - For compose runs they are mounted read-only over the installed modules in the checkout
    image (`proto.yaml`, `desc.yaml`, `atomic_desc.yaml`).
  - For native runs they were copied into the source tree and restored afterwards.
- **Host.** M3 Pro (5 performance + 6 efficiency cores), 18 GB. Docker Desktop VM with
  11 CPUs / 13 GB. All runs were on battery power. That is consistent across runs, but
  absolute numbers are probably lower than on AC power.
- **Validity controls.**
  - **Calibration**: the compose baseline was re-run on Oct 16 and gave 409.2 tx/s vs 409.0
    on Oct 14. The native baselines were also re-run (827 / 818 vs 809 / 787).
  - **Discarded runs**: runs taken while the laptop was asleep, and native runs that hit a
    stale server on the port, were discarded and repeated (*Excluded runs*).
  - **Checks**: every included run passed the stock invariant, with 0 deadlocks and
    0 transport errors.
- **Repetition.** One run per configuration (plus the reruns above). Treat differences under
  ~5 % as noise.

## Results

### Compose stack: 100 stations / 30 s / ample stock

| Run | Change vs. graded compose | tx/s | items/s | scan p50 / p95 | complete p50 / p95 / p99 |
|---|---|---|---|---|---|
| `base100` | — (graded topology) | **409.0** | 4 324 | 9.2 / 23.6 | 47 / 432 / 699 |
| `base100_r3` | — (calibration, Oct 16) | 409.2 | 4 306 | 9.4 / 23.5 | 48 / 428 / 715 |
| `d1_netclient` | load client inside the compose network | 584.3 | 6 122 | 3.9 / 23.0 | 34 / 307 / 494 |
| `d2_pgsock` | checkout → Postgres over a shared Unix socket | 424.2 | 4 492 | 8.6 / 28.5 | 46 / 395 / 648 |
| `d3_w10` | checkout 10 workers × pool 6 (was 6 × 10) | 420.8 | 4 399 | 5.3 / 40.3 | 44 / 360 / 590 |
| `d5_httptools` | uvicorn `--http httptools` behind nginx | 471.7 | 4 944 | 7.6 / 20.5 | 42 / 390 / 645 |
| `p1_atomic` | single-statement completion | **534.8** | 5 612 | 9.3 / 25.9 | 33 / **172** / 282 |
| `p2_desc` | lock stock in descending SKU order | 404.1 | 4 251 | 9.0 / 25.1 | 70 / 378 / 628 |
| `p3_atomic_desc` | single statement + descending order | 509.4 | 5 366 | 10.0 / 28.7 | 40 / 141 / 208 |
| `best_net_atomic_httptools` | in-network client + single statement + httptools | **735.2** | 7 672 | 6.8 / 19.2 | 24 / 137 / 220 |

### Native: host PostgreSQL over the Unix socket, 4 workers × pool 12, ample stock

| Run | Code | Stations | tx/s | items/s | scan mean / p50 / p95 | complete mean / p50 / p95 |
|---|---|---|---|---|---|---|
| `n6_pre005_native_1st` | pre-005 | 1 | 86.8 | 922 | 0.77 / 0.76 / 0.89 | 2.63 / 2.78 / 3.5 |
| `n1_native_1st` | 005 | 1 | 93.6 | 976 | 0.73 / 0.73 / 0.84 | 2.45 / 2.64 / 3.2 |
| `n4_native_atomic_1st` | 005 + single statement | 1 | 89.6 | 954 | 0.75 / 0.76 / 0.86 | 2.51 / 2.68 / 3.4 |
| `n5_pre005_native_10st` | pre-005 | 10 | 687.9 | 7 198 | 1.03 / 0.72 / 1.89 | 2.79 / 2.62 / 5.2 |
| `n2_native_10st` | 005 | 10 | 762.3 | 8 008 | 0.91 / 0.87 / 1.38 | 2.65 / 2.46 / 4.6 |
| `pre005_native100` | pre-005 (Oct 14) | 100 | 787.4 | 8 280 | 7.5 / 6.3 / 17.4 | 40 / 18 / 147 |
| `n10_pre005_native100_rerun` | pre-005 (Oct 16) | 100 | 817.6 | 8 604 | 7.2 / 6.6 / 15.8 | 39 / 17 / 144 |
| `c005_native100` | 005 (Oct 14) | 100 | 808.8 | 8 492 | 7.3 / 5.1 / 18.4 | 40 / 18 / 147 |
| `n7_native100_rerun` | 005 (Oct 16) | 100 | 827.2 | 8 655 | 7.6 / 7.0 / 16.3 | 33 / 16 / 124 |
| `n9_native_desc100` | 005 + descending order | 100 | 851.9 | 8 965 | 7.5 / 3.3 / 18.0 | 31 / 24 / 79 |
| `n3_native_atomic100` | 005 + single statement | 100 | **933.0** | 9 808 | 8.2 / 3.8 / 20.4 | 13 / 11 / **30** |
| `n8_native_atomic_desc100` | 005 + single statement + descending | 100 | 923.2 | 9 697 | 8.2 / 7.5 / 17.3 | 14 / 13 / 29 |

For comparison, the containerized 10-station smoke run (`smoke`, 10 s) had a scan mean of
1.98 ms against 0.91 ms natively. That is **~1.1 ms per request of Docker overhead** with no
lock contention at all.

### Graded stress config (100 stations / 120 s / stock 10 000), compose, host client

| Run | tx/s | items/s | COMPLETE err % (409s) | complete p50 / p95 / p99 | Invariant |
|---|---|---|---|---|---|
| committed report `report-20261013-093519` | 190.9 | 5 242 | 61.7 % | 15 / 225 / 490 | PASS |
| `g_base_stress` (this investigation) | 186.3 | 5 063 | 61.3 % | 16 / 249 / 539 | PASS |
| `g_best_stress`: single statement + httptools | 206.2 | 6 046 | 64.1 % | 16 / **73** / 145 | PASS |
| `g_best2_stress`: single statement + descending + httptools | 211.3 | 6 204 | 64.2 % | 25 / 105 / 166 | PASS |

In this config successful tx/s is partly **stock-bound**: once SKU-000001/2 sell out, most
completions fail fast with `409 INSUFFICIENT_STOCK` by design. Items/s (+19–23 %) and
completion latency show the improvement better than tx/s (+11–13 %).

### Per-layer cost of one scan (`layers_bench.py`, native, one connection, 3 000 iterations)

| Path | Mean | Increment | What the increment is |
|---|---|---|---|
| asyncpg `SELECT 1` | 0.033 ms | — | one client↔Postgres round trip over the Unix socket |
| asyncpg scan CTE, autocommit (prepared) | 0.053 ms | +0.020 | Postgres executing the scan: `UPDATE` + `INSERT`, 2 FK checks |
| asyncpg `BEGIN` + scan + `COMMIT` | 0.109 ms | +0.056 | the two extra round trips of an explicit transaction |
| SQLAlchemy `AsyncSession`: `repo.scan_item` + `commit` | 0.252 ms | **+0.143** | session/transaction state machine, `text()` handling, greenlet bridge, result rows |
| `checkout_service.service.scan_item` | 0.250 ms | ≈ 0 | the service's own logic (catalog lookup, response dict) |
| **end-to-end at one station** (`n1_native_1st`, client-measured) | **0.73 ms** | **+0.48** | Java client, loopback TCP, uvicorn h11 parsing, Starlette routing, FastAPI `Depends(get_session)`, ORJSON |

## Findings

### F1 — Throughput is capped by the hot-SKU lock, and its hold time is set by client round trips

- **Evidence.**
  - Of completed baskets, **67.7 %** contain `SKU-000001` and **82.7 %** contain one of the
    top three. `SKU-000001` is 12.4 % of all scans; the mean basket is 10.4 lines.
  - Wait events per sample, baseline:

    | Deployment | `active / Lock:tuple` | `idle in transaction / ClientRead` |
    |---|---|---|
    | compose | ~30 | ~6 |
    | native | ~17 | ~7 |
  - The idle-in-transaction backends **hold** stock locks while they wait for the
    application to send the next statement.
  - `pg_stat_statements` puts the mean execution time of the `SELECT … FOR UPDATE` lock
    statement at **102 ms** under compose. That is almost entirely waiting; every other
    statement averages 0.003–0.12 ms.
- **Mechanism.**
  - `complete_transaction` takes the stock locks in one deterministic order, then runs:
    1. the decrement
    2. the alert insert, if any
    3. the finalize `UPDATE`
    4. `COMMIT`

    Each step is a separate client round trip, issued while the hot locks are held.
  - Each round trip waits for the checkout worker's event loop to come back round to that
    request. The worker is busy with scans, so that wait is far longer than the 0.03 ms
    network time.
  - The SKU-1 lock is essentially always busy, so completions serialize on it:
    `completions/s ≈ P(basket ∋ SKU-1) / hold_time(SKU-1)`.

    | Deployment | tx/s | implied SKU-1 hold |
    |---|---|---|
    | native | ~810–830 | ≈ 1.8 ms |
    | compose | 409 | ≈ 3.6 ms |
  - The load client is closed-loop, so stations stuck in `COMPLETE` stop scanning. Scan
    throughput falls in step.
- **Why this explains the regression.** Containerization didn't make any one statement slow.
  It made each round trip slower: port forwarding, the virtual NIC, an extra hop, and a
  costlier syscall path on a worker that is already near saturation. The lock multiplies
  that by 3–4 round trips per hold, with ~30 stations queued behind it.
- **Confirming experiments.**
  - **Shortening the hold works.** The single-statement completion (`p1_atomic`, `n3`)
    leaves one round trip (`COMMIT`) after the locks are granted. It does locking, the
    stock check, decrement, alerts and finalize in one CTE; a `MATERIALIZED` locking CTE
    keeps SKU order.

    | Deployment | tx/s | `COMPLETE` p95 | Lock waiters |
    |---|---|---|---|
    | native | 827 → **933** | 124 → **30 ms** | **~18 → ~2** |
    | compose | 409 → **535** | 432 → **172 ms** | ~35 → ~18 |

    Natively the lock stops being the bottleneck; the limit moves to the workers' CPU.
  - **More parallelism doesn't help** (`d3_w10`: +3 %). A serial lock can't use more workers.
  - **Lock order doesn't help.** Descending order takes `SKU-000001` last instead of first:
    `p2_desc` −1 %, `n9_native_desc100` +3 %, and combined with the single statement −5 %.
    - The waiters just move from `Lock:tuple` to `Lock:transactionid`; ~32 still wait under
      compose.
    - A transaction now holds SKU-2/3, also hot, while it waits for SKU-1. After it gets
      SKU-1 it still holds it for the same post-lock round trips.
    - It also gave inconsistent tail effects (p95 sometimes better, p50 worse).
    - The round trips *after* the lock is granted set the hold time, not the order the locks
      are taken in.

### F2 — Docker Desktop's host→VM port forwarding is the largest single deployment cost

- Moving the client into the compose network (`d1_netclient`) gave **+43 % tx/s** (409 → 584).
  Scan p50 fell 9.2 → 3.9 ms. That is about 44 % of the native↔compose gap.
- The 005 README estimated this at ~0.2 ms per request from a 10-station A/B. At 100 stations
  the forwarding proxy is itself a contended userspace hop on the host, and per-round-trip
  cost feeds straight into F1.
- The graded runs must use the host client (FR-008: unmodified client, one base URL), so this
  cost is inherent to running the graded test against Docker Desktop on macOS. A Linux host,
  with no VM and native port publishing, wouldn't pay it.

### F3 — Inside the VM, checkout workers are near CPU saturation and each request costs ~2× more CPU

- **Utilization.**
  - At the stress load the checkout container uses a median **478 %** of its 600 % ceiling
    (6 workers), at ~5 400 requests/s: **~0.9 ms of CPU per request**.
  - Natively, 4 workers serve ~10 000 requests/s: **≤ 0.4 ms per request**.
  - Postgres uses ~1.7–2.5 CPUs and nginx ~0.5–0.65. The VM as a whole takes ~840–970 % of
    the host's 1 100 %, alongside the Java client's ~75–130 %.
- **Pure computation doesn't explain the 2×.** A pure-Python benchmark runs only **~18–20 %
  slower** in the VM (166 vs 200 ms single-threaded; 186–196 vs 224–234 ms with six in
  parallel). The rest is most likely the VM's costlier syscalls and virtual networking: per
  request, ~4 socket operations to nginx plus 3+ Postgres round trips. That attribution is
  inferred; this investigation did not isolate it.
- **Requests queue inside the service.** nginx measures the checkout service's time per scan
  at **8.6–10.2 ms**, while Postgres executes the scan in **0.1 ms**. The rest is queueing
  inside checkout:
  - waiting for the event loop
  - waiting for a pool connection. On average ≈ 41 of 60 connections are tied up in
    completions: 30.4 waiting on `Lock:tuple`, 4.3 on `Lock:transactionid`, and 6.3 holding
    locks while idle in transaction.
- **httptools.**
  - Behind nginx the `Upgrade: h2c` header that forced `--http h11` (001 R14) never reaches
    the service: nginx doesn't forward hop-by-hop `Upgrade` headers. So `httptools` is safe
    there. Empirically, `d5_httptools` served 149 k POST scans and 28 k start/complete POSTs
    with 0 errors.
  - It gave +15 % tx/s, mostly on the scan path (scan p95 23.6 → 20.5 ms).
  - It didn't help `COMPLETE`, which is lock-bound.

### F4 — Request cost is dominated by the Python stack, not the database

- **Round trips per request** (`pg_stat_statements` counts 155 694
  `BEGIN ISOLATION LEVEL READ COMMITTED` for ~155 k requests: SQLAlchemy's asyncpg dialect
  opens every transaction with its own statement):

  | Request | Round trips | Breakdown |
  |---|---|---|
  | scan | **3** | `BEGIN` → CTE → `COMMIT`, though the CTE is atomic on its own |
  | start | 3 | `BEGIN` → `INSERT … RETURNING` → `COMMIT` |
  | complete | **7–8** | `BEGIN` → lock tx → basket → lock stock → decrement → [alerts] → finalize → `COMMIT`; 4–5 of them after the hot locks are taken |
- **Per layer, natively.** Of a 0.73 ms scan, Postgres's work plus one round trip is
  0.05 ms. Explicit transaction framing adds 0.06 ms, SQLAlchemy 0.14 ms, and the HTTP stack
  ~0.48 ms. The service code is free.
- **Profile of one checkout worker under compose load**
  (`results/pyspy_checkout_worker.txt`; on-CPU samples, inclusive, so categories overlap):

  | Layer | Share |
  |---|---|
  | uvicorn + asyncio loop (`run`, socket I/O; uvloop C code) | ~28 % self |
  | SQLAlchemy (session state machine, `text()` compile/caching, greenlet bridge, result processing) | ~28 % |
  | h11 (pure-Python HTTP/1.1 parser) | ~26 % |
  | FastAPI / Starlette (routing, `Depends` solving, middleware) | ~21 % |
  | asyncpg (protocol, prepared-statement execution) | ~9 % |
  | `checkout_service` + `checkout_data` (the app's own code) | ~9 % / ~7.5 % |
- **Without contention a completion costs ~2.5 ms,** ≈ 3.4 scans' worth. That fits 7–8
  round trips, each carrying the SQLAlchemy overhead.
  - The single-statement prototype doesn't change that number (2.51 vs 2.45 ms at one
    station).
  - The round trips it removes are cheap natively. Its larger statement is planned per
    basket size, and that roughly cancels the savings.
  - Its benefit is entirely in **shorter lock hold under contention**.

### F5 — Ruled out

- **nginx processing**: `$request_time − $upstream_response_time` averages 0.001–0.019 ms per
  route. The *hop itself* (an extra container-to-container connection) is part of F3's
  unattributed VM cost.
- **Postgres over TCP** (research R8): +4 %, within noise. Sharing the Unix socket isn't worth
  the compose complexity.
- **The 005 code changes** (persistent analytics pipeline, repo/service split, `checkout-data`):
  natively the 005 code is as fast as pre-005, or slightly faster: 809/827 vs 787/818 tx/s at
  100 stations, 762 vs 688 at 10, 0.73 vs 0.77 ms per scan at 1.
- **Analytics tail ingest**: 300 range queries per 30 s, 90 ms of DB time in total. The
  analytics container uses 10–18 % of one CPU.
- **Pool / worker sizing**: 10 × 6 instead of 6 × 10 changed nothing.
- **Lock acquisition order**: see F1. Any deterministic order has the same post-lock hold.
- **Query plans and I/O**: every hot statement is an index lookup. `pg_stat_database` shows
  `blks_read` of 56–61 per run; the working set is fully cached.

## Sources of latency in the app as written (summary)

| Source | Where | Cost | Matters most |
|---|---|---|---|
| Hot-SKU row lock held across 3–4 client round trips | `checkout_service.service.complete_transaction` steps 3–6 | `COMPLETE` p95 124 ms native / 432 ms compose, vs 30 / 172 ms with the critical section in one statement | under concurrency: sets system throughput |
| HTTP stack: h11 parser, Starlette routing, FastAPI dependency injection | every request | ~0.48 ms of 0.73 ms per scan natively; ~47 % of worker CPU under load | always: the floor on per-request latency, and what saturates workers |
| SQLAlchemy `AsyncSession` around raw `text()` SQL | every request | +0.14 ms per scan (more than all DB work); ~28 % of worker CPU | always |
| Explicit transactions for single-statement requests | scan, start (`session.commit()` after one statement) | +2 round trips, +0.06 ms natively, more in the VM | always; grows with network cost |
| Completion as 7–8 sequential statements | `complete_transaction` | ~2.5 ms uncontended | under contention (see row 1) |
| Database execution | all | 0.02–0.12 ms per statement | rarely |

## Recommendations (ranked by measured impact ÷ risk)

1. **Shorten the completion's critical section** (prototype: `harness/proto/`).
   - Do lock → check → decrement → alerts → finalize as one statement.
   - It keeps:
     - all-or-nothing semantics
     - deterministic SKU lock order (a `MATERIALIZED` locking CTE evaluated first)
     - alerts committed with the decrement
     - the stock-never-negative check
   - It passes all 77 contract/integration tests and the invariant on every load run, with
     0 deadlocks.
   - Measured:
     - native: +13 % tx/s, `COMPLETE` p95 −76 %
     - compose: +31 % tx/s, p95 −60 %
     - graded stress: p95 −71 %, items/s +19 %
2. **Use `--http httptools` for services behind the gateway** (+15 % under compose). The
   contract and integration tests run in-process and don't exercise the parser, so re-run the
   gateway-mode suite (`GATEWAY_URL`) before adopting it. Keep `h11` anywhere the Java client
   connects directly.
3. **Run single-statement requests in autocommit** (scan, start). That removes `BEGIN` and
   `COMMIT`: 3 → 1 round trips for ~90 % of requests, and ~0.06 ms per scan natively,
   presumably more in the VM. Not load-tested here.
4. **Optional, larger: drop the ORM session on the hot paths.** The SQL is already hand-written
   `text()`, so executing it on a raw asyncpg pool connection removes ~0.14 ms per request and
   about a quarter of worker CPU. It does give up some of the repo layer's uniformity.
5. **Document the measurement setup's cost honestly.** Docker Desktop port forwarding is ≈ 44 %
   of the gap at 100 stations, not ~0.2 ms. It is a property of the macOS test setup, not the
   architecture. Update the comparability note in `load-client/reports/README.md`.
6. **Not worth it**: the Postgres Unix socket (R8), more checkout workers, nginx tuning, or
   changing lock order.

## Excluded runs

| Run | Why |
|---|---|
| First `d3_w10`, `d3_w10_r2`, `d5_httptools`, `base100_r2` (Oct 14) | The laptop was in clamshell/dark-wake sleep during these runs: 94–495 client timeouts, and the VM got ~555–600 % CPU instead of ~840 %. A baseline re-run in that state gave 232 tx/s instead of 409. Repeated on Oct 16. |
| First `n1`–`n4` (Oct 16) | Orphaned uvicorn workers from an Oct 14 native run still held port 8090. The new server failed to bind (`Errno 48`), and the client load-tested the stale 2-worker server, capped at ~300 tx/s. The orphans were killed, and `native_exp.sh` now aborts if the port is busy and kills all workers on exit. Repeated. |
| `pyspy` run | Profiling perturbs throughput. Used only for the CPU breakdown. |

## Reproducing

```sh
# from backend/, Docker Desktop running, load client built (load-client/build.sh)
H=specs/006-service-perf-investigation/harness
# The scripts hard-code this investigation's scratch paths (J, BACK, LC, WT at the top of
# each script, and the mount paths in *.yaml). Point them at your checkout first, then:
$H/run_exp.sh base100                                          # graded topology
CLIENT=net $H/run_exp.sh d1_netclient                          # client inside the network
EXTRA="-f $H/proto.yaml" $H/run_exp.sh p1_atomic               # single-statement completion
EXTRA="-f $H/httptools.yaml" GWLOG=1 $H/run_exp.sh d5_httptools
STATIONS=100 DURATION=120 STOCK=10000 $H/run_exp.sh g_base_stress
.venv/bin/python $H/layers_bench.py                            # needs a seeded checkout_perf DB
```

Per-run summaries, statement statistics and the load-client reports are in `results/<run>/`.

## Outcome (spec 006 implementation)

**Date**: 2026-10-16, branch `006-hot-path-latency`.

**How it was run**: every number below comes from one session, on battery power, with no
sleep events.

- `*_pre` is the calibration arm, with application code identical to `origin/main`. It
  reproduced the investigation baseline: 414.5 vs 409 tx/s.
- Each later arm adds one user story:
  - `_us1`: single-statement completion
  - `_us2`: plus autocommit start/scan
  - `_us3` / `_006`: plus `httptools` behind the gateway; `_006` is the final code

  Native runs use `h11`, so US3 does not apply to them.
- Every run passed the invariant, with 0 deadlocks, 0 client timeouts and 0 5xx.
- Per-transaction time comes from `harness/cycle.py` (closed-loop accounting from the
  load-client report).

### Compose, 100 stations / 30 s / ample stock

| Run | tx/s | items/s | scan p50 / p95 | `COMPLETE` p50 / p95 / p99 | ms per tx: start + scans / complete / total | `BEGIN` per completion | Postgres CPU |
|---|---|---|---|---|---|---|---|
| `base100_pre` | 414.5 | 4 322 | 9.1 / 24.9 | 45 / 432 / 679 | 127 / 114 / **241** | 12.5 | 183 % |
| `base100_us1` | **559.8** | 5 910 | 9.3 / 24.3 | 27 / **146** / 252 | 132 / 47 / **178** | 12.6 | 253 % |
| `base100_us2` | 560.7 | 5 925 | 6.5 / 26.6 | 37 / 224 / 355 | 111 / 67 / **178** | **1.04** | 190 % |
| `base100_us3` | **618.2** | 6 420 | 5.8 / 26.9 | 33 / 198 / 317 | 102 / 59 / **162** | 1.03 | 213 % |

### Native (one process, Unix socket), 100 stations / 30 s / ample stock

| Run | tx/s | items/s | `COMPLETE` p50 / p95 / p99 | ms per tx: start + scans / complete / total |
|---|---|---|---|---|
| `native100_pre` | 804.8 | 8 464 | 17 / 147 / 248 | 84 / 41 / **124** |
| `native100_us1` | 884.3 | 9 258 | 11 / **25** / 35 | 101 / 12 / **113** |
| `native100_006` | **993.6** | 10 419 | 15 / 63 / 104 | 78 / 22 / **101** |

### Graded stress config (100 stations / 120 s / stock 10 000), compose

| Run | tx/s | items/s | COMPLETE err % (409s) | `COMPLETE` p50 / p95 / p99 |
|---|---|---|---|---|
| `g_stress_pre` | 188.7 | 5 091 | 61.1 % | 16 / 242 / 514 |
| `g_stress_006` | **219.1** | **6 690** | 65.6 % | 15 / 127 / 267 |
| graded report `006-hot-path-latency/report-20261016-082910` | 224.5 | 6 804 | 65.4 % | — / 122 / — |

The regenerated default-parameters report (10 stations / 60 s) gives 262.5 tx/s and
5 615 items/s, against Week 5's 220.6 and 4 250.

### Success criteria

| Criterion | Target | Result | Met? |
|---|---|---|---|
| SC-001 compose tx/s | ≥ 500 | 618.2 (`base100_us3`) | ✅ |
| SC-001 compose `COMPLETE` p95 | ≤ 200 ms | 197.6 ms (146 ms after US1 alone) | ✅ (barely) |
| SC-002 graded items/s | ≥ 5 800 | 6 690 | ✅ |
| SC-002 graded `COMPLETE` p95 | ≤ 100 ms | 127 ms | ❌ |
| SC-003 native `COMPLETE` p95 | ≤ 40 ms | 63 ms (25 ms after US1 alone) | ❌ |
| SC-004 `BEGIN` ≤ 1.1 × completions | ≤ 1.1 | 1.03 | ✅ |
| SC-005 invariant / deadlocks / errors | PASS / 0 / 0 | PASS / 0 / 0 on every run | ✅ |

### Reading the two misses

**What US1 delivered.** US1 did what the investigation predicted: it shortened the hot-lock
hold, and with it the whole transaction cycle (compose 241 → 178 ms, native 124 → 113 ms).

**What US2 and US3 changed.** They then made the *non-lock* part of the cycle cheaper.

- **Under compose, with US2:** throughput was still capped by the SKU-000001 lock, which is
  hit by 68 % of baskets. So the freed time didn't shorten the cycle (178 → 178 ms). It
  moved into the lock queue: stations got to `COMPLETE` sooner and waited there longer.
- **Natively, and under compose with US3:** some of the saving became real throughput
  (native +12 %, compose +10 %). The rest became more lock queueing.

**Why the criteria were the wrong yardstick.** The `COMPLETE` p95 targets were set from the
prototype runs, which changed only the completion and so held the rest of the cycle fixed.
Under a closed-loop client whose bottleneck is a serial lock, faster *other* requests
legitimately *raise* time spent waiting at the bottleneck. The end-to-end measures (tx/s,
items/s, per-transaction cycle) improved at every step except US2 under compose, where they
were flat. That step still cut Postgres CPU from 253 % to 190 % and per-request round trips.

**Options** (a decision for the spec owner):

1. Keep all three changes, and restate SC-002/SC-003 in throughput terms. They're met that
   way: items/s +31 % graded, tx/s +23 % native.
2. Revert US2 to recover US1-level `COMPLETE` p95 natively. That gives up native +12 % tx/s,
   SC-004 and the CPU saving.
3. Keep the code, and attack the remaining serial bottleneck itself. Contention on the
   SKU-000001 row is now the limit, not round trips.
