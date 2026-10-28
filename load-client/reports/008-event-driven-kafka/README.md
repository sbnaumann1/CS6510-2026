# Week 8: event-driven microservices over Kafka (2026-10-27)

The two graded load-client reports for [spec 008](../../../backend/specs/008-event-driven-kafka/spec.md),
run against the compose stack with the **unmodified** load client and the assignment's flags
(`./run.sh` wraps `java -cp out Main "$@"`):

| Report | Command | Config |
| --- | --- | --- |
| [`report-20261027-220244.json`](./report-20261027-220244.json) | `java -cp out Main` (default parameters) | 10 stations, 60 s |
| [`report-20261027-220513.json`](./report-20261027-220513.json) | `java -cp out Main --stations=100 --duration=120` (stress mode) | 100 stations, 120 s |

The architecture diagram: [`backend/docs/architecture.md`](../../../backend/docs/architecture.md)
(renders in place on GitHub; the same picture is
[`architecture.svg`](../../../backend/docs/architecture.svg)).

Each run started from `docker compose down -v` and `up --build --wait`, so the stack was fresh,
and each was followed by the cross-store check (`tools/verify_invariant.py`), which **passed
both times**: `initial_stock − current_stock == completed line items` for every SKU, no negative
stock, no transaction stuck `COMPLETING`, no outbox row left unpublished, every scan relayed to
the topic and read by Analytics.

## Results

| | Default (10 / 60 s) | Stress (100 / 120 s) |
| --- | --- | --- |
| Completed transactions | 9 981 | 19 261 |
| **Transactions/s** | **166.2** | **160.2** |
| Items scanned/s | 2 554 | 3 858 |
| `START_TRANSACTION` / `SCAN_ITEM` errors | 0 / 0 | 0 / 0 |
| `COMPLETE_TRANSACTION` errors | 4 632 (31.7 %) | 24 906 (56.4 %) |
| `COMPLETE_TRANSACTION` mean / p50 / p95 / p99 | 21.6 / 20.7 / 32.4 / 42.8 ms | 183.3 / 173.4 / 303.1 / 388.4 ms |
| Invariant | PASS | PASS |

**Every one of the `COMPLETE_TRANSACTION` errors is the contract's `409 INSUFFICIENT_STOCK`.**
The logs show exactly as many Inventory `stock-rejected` replies as the report shows errors
(4 632 and 24 906), and no timeout, re-publish, compensation, dead letter or traceback in
either run. Zero transport failures, zero server errors (SC-003). As in every earlier week, the
hot SKUs sell out partway through a run at the default 10 000 units each, and later baskets
containing them must be refused rather than oversold.

## Against Week 7 (SC-010)

Same machine, same stock, same load client; Week 7's reports are in
[`../007-microservices-architecture/`](../007-microservices-architecture/).

| | Week 7 (HTTP between services) | Week 8 (events over Kafka) | Ratio |
| --- | --- | --- | --- |
| Default: tx/s | 200.5 | 166.2 | **83 %** |
| Stress: tx/s | 196.2 | 160.2 | **82 %** |
| Default: `COMPLETE` mean | 10.2 ms | 21.6 ms | 2.1× |
| Stress: `COMPLETE` mean | 80.0 ms | 183.3 ms | 2.3× |
| `SCAN_ITEM` mean (default / stress) | 1.6 / 10.1 ms | 1.7 / 7.3 ms | same / better |

Both are above the 70 % line the spec set, so no shortfall needs defending, but the gap is real
and its cause is measured, not guessed.

**Where the time goes.** Scans and starts are unchanged (they never touched another service,
and now do not publish an event on the request path). Only completion changed: the HTTP call to
Inventory became publish-request, wait-for-reply. From the services' own step logs
(`scripts/step_timings.py logs/compose-stress.log`):

| Step (stress run) | Mean | p95 |
| --- | --- | --- |
| Inventory handles one reservation (its database transaction) | 10.3 ms (21.4 ms when it refuses) | 28.7 / 43.0 ms |
| Checkout waits for the reply event, request to reply | 147 ms (173 ms for a refusal) | 249 / 278 ms |
| Checkout's local transactions (begin, finalize) | 14.6 ms, 8.4 ms | 37.5 / 25.5 ms |

So of Checkout's ~150 ms wait, Inventory's own work is ~10–20 ms. The rest is **queueing for
Inventory's consumers**: two Inventory worker processes each handle up to six events at once
(bounded by their database pool), against 100 concurrent stations, plus two broker hops of a few
milliseconds each. At 10 stations there is no queue and the cost is just the two hops (about
+11 ms per completion), which is the default run's whole difference. The obvious tuning, not
done here because the result already meets the target and the invariant matters more than the
last few tx/s, is more Inventory consumer concurrency (a larger pool and handler concurrency, or
more workers).

## What the event path looked like in a quiet system

For one completion (`./scripts/trace_request.sh`): the request id appears in the Checkout and
Inventory logs and on both Kafka events, and the whole round trip is about 11 ms.

## Drills run on the same build (all PASS)

| Drill | Result |
| --- | --- |
| `check_isolation.sh` | no service can resolve a peer; all reach Kafka; no service reaches another's store |
| `check_no_peer_calls.sh` | 63 shopping sessions through the gateway, every endpoint 2xx, no service ever connected to another |
| `check_independence.sh` | each service recreated alone, then stopped in turn (and Kafka): only dependants fail, in the contract's error shape, refusals within 5 s; Catalog down does not stop a restarted Checkout; Analytics rebuilds without Checkout |
| `check_crash_recovery.sh` | Checkout, then Inventory, killed under load: 4 549 units decremented = 4 549 completed line items, 0 outbox rows pending, 5 107 / 5 107 scans relayed |
| `check_broker_outage.sh` | Kafka stopped for 15 s under load: 8 641 = 8 641, 0 outbox rows pending, 9 292 / 9 292 scans relayed, 0 dead letters |

The outage drill is worth a note: its first run **failed**, and found a real defect. After the
broker restarted, aiokafka's idempotent producer wedged for good (`OutOfOrderSequenceNumber`),
so Checkout's scan relay stopped and Analytics fell behind (6 301 of 6 664 scans). The fix was to
publish without producer idempotence (delivery is at-least-once by design and every consumer is
idempotent) and to have the bus replace a producer after a fatal producer error. The drill then
passed. The independence drill likewise found that a completion with Kafka down took 15 s, not
under 5, because each publish waited for its own timeout before the wait began; publishing now
happens inside the wait.
