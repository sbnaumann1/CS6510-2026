# Spec 009: where the time goes, and what speeds the system up

Week 8 (spec 008, events over Kafka) ran at 166 tx/s (default) and 160 tx/s (stress), about
83 % of Week 7's 200 / 196. This document records the investigation into why, what was tried,
what was measured, what shipped and what did not. Every number is from the unmodified Java load
client against the compose stack unless a section says otherwise. Raw logs are in
`logs/profile*/` (not committed); the tools that produced them are in `scripts/`.

## Method

- **Wall-clock trace points** (`checkout_web/trace.py`, `TRACE_TIMING=1`, off by default) along
  the completion path, joined by transaction id by `scripts/profile_completion.py`.
- **Saturation probes** (`checkout_web/diagnostics.py`): per-worker event-loop lag, database
  pool use and wait, plus `docker stats` CPU per container (`scripts/sample_stats.sh`,
  `scripts/summarize_diagnostics.py`).
- **`scripts/run_profile.sh`**: fresh stack per run, one named configuration, the Java client,
  then the breakdowns. Runs were 60 s with stock raised so no basket is refused (the graded runs
  refuse about a third of completions at default load, which changes `tx/s`).
- Run-to-run noise is about ±5-10 %. Most comparisons below are two runs per configuration.

## 1. How throughput is made

The load test is closed-loop. Each station repeats: start, about 10.5 scans, complete. So

    tx/s = stations / (start + 10.5 x scan + complete)

which matched the measured figure within 1 % (stress baseline: 307.7 predicted, 307.2 measured).
Two consequences drove the rest of the investigation:

- Per transaction, Checkout serves about **12.5 requests, 84 % of them scans**. Under stress,
  `tx/s` is Checkout's request capacity divided by 12.5.
- Completion is the slowest request (about 180 ms under stress) but only about 56 % of a cycle
  and 1 request in 12.5. Making it faster lets stations send scans sooner into a service that is
  already saturated, so scans slow down and throughput barely moves.

## 2. Where one completion spends its time

Mean per step (ms), one completion, from the trace points:

| Step | 10 stations | 100 stations |
| --- | --- | --- |
| Checkout `begin` transaction | 3.6 | 24.6 |
| Schedule the publish | 0.2 | 1.1 |
| **Publish start to Inventory handler start** | **9.3** | **88.8** |
| Inventory database transaction | 2.6 | 13.7 |
| Inventory publishes the reply (ack) | 3.3 | 5.8 |
| Reply reaches Checkout's consumer | 0.9 | 15.2 |
| Waiter resumes the request | 0.3 | 2.6 |
| Checkout sets `RESERVED` | 1.8 | 15.4 |
| Checkout finalizes | 1.7 | 14.9 |
| **Sum** (measured inside the saga: 23.6 / 181.0) | **23.7** | **182.0** |

The sweeper, recovery loop and re-publishes never ran on the happy path (zero in every run), and
offset commits are cheap (1-2 ms).

## 3. What the wait for Inventory is made of

The 89 ms (stress) between Checkout publishing and Inventory's handler starting is **queueing,
not Kafka transit**: inside a polled batch a record waits only about 12 ms. Three things feed it,
measured by changing one at a time (stress, 2 runs each):

| Configuration | Wait for handler | `COMPLETE` mean | tx/s |
| --- | --- | --- | --- |
| baseline (batch loop, row locks) | 76-77 ms | 181-186 ms | 303-311 |
| no stock-row contention (test-only) | 44-60 | 150-168 | 280-327 |
| pipelined loop, row locks | 51-52 | 171-174 | 301-304 |
| pipelined, no contention (test-only) | 27 | 141-142 | 320-323 |
| pipelined, 12 handlers, row locks | 38-69 | 181-205 | 292-293 |

- **The consumer loop** (poll a batch, handle all of it, commit, poll again) was about 97 % busy:
  new requests waited for the batch's slowest record plus a commit. A loop that does not wait
  for a batch (offsets committed by watermark) cut the wait by about a third.
- **Hot-SKU row locks are real under the real load**: `inventory-db` had a mean of 1.1-1.9
  backends waiting on a lock, up to 9, in 37-68 % of one-second samples. (An earlier lock test
  using my own Python load generator saw none: the generator, not the system, was the limit.)
  Removing all stock-row contention made Inventory's database step 55 % faster.
- **They cap pipelining.** With locks in place, more concurrent handlers only add lock waiters
  (database step 35-47 ms at 12 handlers).
- A bare `UPDATE` without ordered locking is not an alternative: it deadlocked 30-38 times per
  run (each costing a second), and an `UPDATE` holds its row lock until commit anyway.
- About 20 ms of what remains is Checkout's own producer ack and a slow reply pickup on busy
  event loops: it is Checkout-side.

None of this is shipped. The no-write mode violates the stock invariant by design; the pipelined
loop passes the in-process suites but was not drilled against real containers. Both are kept on
branch `009-inventory-experiments`. A correctness-preserving way to get most of the lock gain is
**micro-batching reservations** (lock the union of a polled group's SKUs once, in SKU order,
apply each request with its own check, commit once); it needs a design pass.

## 4. What did not help (and why)

| Change | Result | Reason |
| --- | --- | --- |
| `acks=1` | none | one broker, already fast |
| consumer fetch wait 500 to 20 ms | none | data already available |
| Inventory 4 workers x 12 handlers | none under stress | lock-bound, see above |
| Checkout pool 10 to 16 | none | pool is full under 1 % of the time; most "pool wait" is event-loop scheduling |
| Checkout 6 to 9 workers | none (3,489-3,689 req/s against 3,783-3,870) | the Docker VM has 11 cores, the stack used 9.8 of them and the Java client shares them |
| Lighter scan SQL (plain read instead of the counter `UPDATE`) | 8 % slower in `pgbench` | the counters are cheap in-place updates; recomputing them costs more |

The Docker VM cannot be given more than the host's 11 logical CPUs.

Balancing Checkout's workers (keep-alive connections pin load to the worker that accepted them:
a 2.1x spread became 1.3x after limiting upstream connection lifetime) improved the pool and
loop-lag figures but not throughput, because total CPU is unchanged.

## 5. What helped

| Change | Default load | Stress |
| --- | --- | --- |
| **Scan fast path** (below) | tx/s +14 % (207 to 236), scan -15 % | tx/s +19 % (297 to 353), scan -30 % |
| Checkout producer `linger_ms=0` | `COMPLETE` -19 % (25 to 20 ms), tx/s about +4.5 % | within noise |
| Reply filtered by record key before decoding | no measurable change | no measurable change |

**The scan fast path** attacks the real limit: CPU per request. Start and scan, which are 92 %
of Checkout's requests, are served by raw-ASGI handlers in front of FastAPI and run their single
statement through a bare asyncpg pool with prepared statements (no `AsyncSession`, no
async-to-sync bridge, no ORM row). Everything else, including every error, still goes through
FastAPI. Measured CPU per request fell from 1.17 to 0.85 ms (stress) and from 1.24 to 0.86 ms
(default), about 30 %. `tests/integration/test_hotpath.py` sends every request to both paths and
requires the same status, headers and body (error bodies byte for byte);
`CHECKOUT_FASTPATH=0` switches it off.

## 5a. Graded runs

The standard graded runs (`./run.sh` and `./run.sh --stations=100 --duration=120`, stock 10 000
per SKU, tracing off), final configuration of this spec, with the invariant check passing on all
of them. The machine is not the same today as on 27 October (the unchanged Week 8 code scores
about 4 % lower), so the fair baseline is the Week 8 code **re-run in the same session**:

| | tx/s | items/s | requests/s | scan mean | `COMPLETE` mean | p99 |
| --- | --- | --- | --- | --- | --- | --- |
| **10 stations** | | | | | | |
| Week 8 report (27 Oct) | 166.2 | 2,554 | 3,041 | 1.7 ms | 21.6 ms | 43 |
| Week 8 code, re-run today | 160.2 | 2,291 | 2,728 | 1.9 | 24.2 | 56 |
| Week 9 without producer linger change | 170.5 | 2,722 | 3,240 | 1.5 | 21.9 | 48 |
| **Week 9 final** | **180.3** | **2,885** | **3,440** | **1.5** | **19.5** | **43** |
| **100 stations** | | | | | | |
| Week 8 report (27 Oct) | 160.2 | 3,858 | 4,593 | 7.3 | 183.3 | 388 |
| Week 8 code, re-run today | 153.0 | 3,467 | 4,127 | 12.4 | 163.4 | 363 |
| Week 9 without producer linger change | 163.4 | 3,894 | 4,636 | 8.5 | 174.9 | 377 |
| **Week 9 final** | **168.0** | **4,161** | **4,951** | **7.9** | **162.1** | **343** |

Against the same-session baseline, Week 9 is **+12.5 % tx/s at 10 stations and +9.8 % at 100**,
and +26 % and +20 % in items and requests per second. Scans are 36 % faster and starts 40 %
faster under stress. Attribution (same session): the Checkout changes without the linger change
give +6 % tx/s and +12-19 % requests/s; the producer `linger_ms=0` adds another +5.7 % (default)
and +2.8 % (stress) tx/s.

`tx/s` understates the gain because it counts only completions that succeed. The hot SKUs sell
out of their 10 000 units sooner when the system is faster, so a larger share of completions is
refused (stress: 53.7 % to 57.5 %) and those do not count, although they are served. Items and
requests per second show the speed-up without that effect. Raw reports:
`load-client/reports/009-kafka-optimization/` (final) and its `controls/` (the re-run and the
linger-2 pass).

Week 7 for reference: the 008 report compared against 200.5 / 196.2 tx/s, while the Week 7 report
files in this repository show 194.4 / 176.9. Week 9 final is 90 % / 86 % of the first pair and
93 % / 95 % of the second.


## 6. Corrections made along the way

- Start and scan did not get slower in Week 8; only completion did (+11 ms at default load).
  A first conclusion that "the other operations slowed down" came from my own Python load
  generator being the bottleneck.
- "Pool wait" was initially read as connection queueing. With the pool almost never full, it is
  mostly event-loop scheduling delay, so enlarging the pool changed nothing.
- Tracing (`TRACE_TIMING=1`) costs about 5-7 % (two untraced runs completed 164-166 ms against
  176 ms traced); the graded runs have it off.

## 7. What is left

- Checkout still spends about 0.85 ms of CPU per request. Next candidates: profile a worker
  with `py-spy` (not done), merge the `RESERVED` and `finalize` transactions (about 2 ms and a
  pool checkout per completion), batch the outbox "mark published" writes, trim per-step logging.
- On the Inventory side, the micro-batching reservation design above is the safe route to the
  lock gain, and sharding hot SKUs' stock across rows is the other.
- At default load completion latency does set throughput, so Inventory work matters for the
  graded 10-station run even though it does not under stress.
