# Contract: Completion Flow over Events

Orchestrator: **Checkout** (`saga.py`). Participant: **Inventory** (`handlers.py`). Public entry
point: `POST /transactions/{id}/complete`. This replaces the transport of
[007's completion saga](../../007-microservices-architecture/contracts/completion-saga.md); its
states, epochs, lease, error precedence and recovery rules still apply except where this page says
otherwise. Event shapes: [events.md](./events.md). Data: [data-model.md](../data-model.md).

## Happy path

```text
Shopper     Gateway   Checkout (checkout-db)           Kafka                   Inventory (inventory-db)
  │ POST complete │        │                              │                              │
  │──────────────▶│───────▶│ 1. LOCAL TX                  │                              │
  │               │        │    lock tx, require OPEN     │                              │
  │               │        │    tx → COMPLETING           │                              │
  │               │        │    completion(RESERVING, n)  │                              │
  │               │        │    outbox(reservation-req)   │                              │
  │               │        │    COMMIT                    │                              │
  │               │        │ 2. register waiter (tx, n)   │                              │
  │               │        │    publish ─────────────────▶│ checkout.completions.v1      │
  │               │        │    (mark outbox published)   │ ────────────────────────────▶│ 3. LOCAL TX, ONE STATEMENT
  │               │        │                              │                              │    recorded? replay outcome
  │               │        │                              │                              │    lock rows (SKU order)
  │               │        │                              │                              │    check, decrement, alerts
  │               │        │                              │                              │    adjustment(APPLIED)
  │               │        │                              │                              │    outbox(stock-reserved)
  │               │        │                              │                              │    COMMIT (locks released)
  │               │        │                              │◀──────────── publish ─────────│ 4. publish, mark published,
  │               │        │◀── inventory.stock.v1 ───────│                              │    commit the offset
  │               │        │ 5. waiter resolves           │                              │
  │               │        │    completion → RESERVED     │                              │
  │               │        │ 6. LOCAL TX                  │                              │
  │               │        │    tx → COMPLETED            │                              │
  │               │        │    completion → COMPLETED    │                              │
  │◀──────────────│◀───────│ 200 Receipt                  │                              │
```

Pre-saga checks keep the 005/007 error precedence (unknown transaction `404`, not open `409
TRANSACTION_NOT_OPEN`, empty basket `409 EMPTY_BASKET`), all inside step 1 before any change.

## Outcomes and compensation

Letters match 007's table so the failure tests carry over.

| # | Situation | Compensation | Final transaction state | Shopper sees |
| --- | --- | --- | --- | --- |
| A | `inventory.stock-rejected` | None (nothing changed in Inventory). Completion `ABORTED` | `OPEN` | `409 INSUFFICIENT_STOCK` (from `shortSkus`) |
| B | No outcome within the deadline | Re-publish the request (twice, still idempotent). If still silent: `COMPENSATING`, publish `stock-release-requested`, await `stock-released`, reopen, `ABORTED` | `OPEN` | `503 INTERNAL_ERROR` |
| C | `inventory.reservation-refused` | Reopen, `ABORTED` (a release is already recorded) | `OPEN` | `503 INTERNAL_ERROR` |
| D | Step 6 fails after `stock-reserved` | `COMPENSATING`, release as above, reopen, `ABORTED` | `OPEN` | `503 INTERNAL_ERROR` |
| E | The release outcome does not arrive (Inventory or broker down) | Stay `COMPENSATING`. The outbox still holds the release request and the sweeper keeps publishing it; the shopper's next `complete`, or the recovery loop, finishes it | `COMPLETING` (reported as `OPEN`) until delivered | `503 INTERNAL_ERROR`, then the retry succeeds |
| F | Checkout crashes in `RESERVING` | Recovery: publish release, await, reopen | `OPEN` | (connection lost) retry |
| G | Checkout crashes in `RESERVED` | Recovery: finalize (roll forward) | `COMPLETED` | (connection lost) `GET` shows `COMPLETED` |
| H | Inventory crashes mid-statement | Its local transaction rolls back; the offset is not committed; on restart the request is redelivered and handled once | `OPEN` or `COMPLETED` | `503`, or success after the redelivery if within the deadline |
| I | **Broker unavailable on publish** | Step 1's outbox row is already committed: the transaction stays `COMPLETING`, the sweeper publishes when the broker returns | `COMPLETING` (reported as `OPEN`) until published | `503 INTERNAL_ERROR` after the deadline; a retry finds the completion owned by the lease or takes it over (007 rule 5) |
| J | Duplicate or late outcome with no waiter | Ignored by the reply consumer | unchanged | none |
| K | `stock-reserved` arrives for an epoch Checkout already aborted | Ignored by the reply consumer: the release Checkout published is ordered after the request on the same key, so Inventory has already undone it or will | unchanged | none |

## What is different from 007

| 007 | 008 |
| --- | --- |
| `POST /internal/reservations` and its response | publish `stock-reservation-requested`, await the reply event |
| `DELETE /internal/reservations/{id}` | publish `stock-release-requested`, await `stock-released` |
| Timeout = HTTP read timeout | Deadline on the waiter; the request is re-published within it |
| Inventory down → connection refused | Inventory down → the request waits in the broker and is handled when it returns; the shopper still gets a clean `503` at the deadline |
| A rejected reserve left no record | A rejected reserve records `REJECTED`, so repeats replay the refusal |

## Idempotency rules

1. **Key**: `(transaction, epoch)`, as in 007. Reserve applies at most once; release reverses at
   most once and always records, so a late reserve of that epoch is refused; a later epoch is not
   blocked.
2. **A repeated request re-emits the recorded outcome** (`replayed: true`). This is the one way
   Checkout asks "what happened?": it never queries Inventory.
3. **Finalize** is guarded by `status = 'COMPLETING'`; a second driver finds the work done.
4. **Waiters** are keyed by `(transaction, epoch)` and are resolved at most once; later copies of
   the same outcome are dropped.
5. A second `complete` while a live driver is working (completion touched within the 10 s lease) is
   refused with `409 TRANSACTION_NOT_OPEN`, as in 007. After the lease expires a retry takes the
   saga over from its recorded state using only idempotent steps.
6. Rules 6–8 of 007 (`COMPLETED`/`CANCELLED` → `409`, scan on `COMPLETING` → `409`, a
   `COMPENSATING` record is finished first) are unchanged.

## Ordering guarantees the flow relies on

| Need | Guarantee |
| --- | --- |
| Reserve is seen before release of the same epoch | both events have key = transaction id → same partition → produced order is consumed order |
| Release recorded before a late reserve is refused | 007's adjustment record, unchanged |
| Outcome for the waiter is not missed | the waiter is registered **before** the request is published, and the reply consumer is assigned and positioned before the worker reports healthy |
| An outcome missed while a worker was down is recoverable | the recovery loop re-requests; Inventory re-emits |

## Recovery loop

Unchanged from 007 (every 2 s, per candidate, `FOR UPDATE SKIP LOCKED`, lease 10 s). Its actions use
the event steps:

| State | Action |
| --- | --- |
| `RESERVING` | publish release, await `stock-released`, reopen, `ABORTED` |
| `RESERVED` | step 6 (finalize) |
| `COMPENSATING` | publish release, await `stock-released`, reopen, `ABORTED` |

The sweeper (outbox) and the recovery loop are independent: the sweeper makes sure every committed
event reaches the broker; recovery makes sure every stuck completion reaches an end state.

## Timing budget (FR-014, SC-009)

| Step | Limit |
| --- | --- |
| Step 1 / 6 local transactions | database statement timeout (5 s) |
| Wait for an outcome | 1.125 s, re-publish, 1.125 s, re-publish, 0.75 s: **3 s total** (`RESERVE_BUDGET_S`) |
| Release after a silent reserve | one more wait of 1.5 s (`RELEASE_WAIT_S`) |
| Whole synchronous `complete` | ≤ 4.5 s before `503`, inside SC-009's 5 s |
| Outbox sweeper | republishes rows unpublished after 2 s, every 1 s |
| Recovery lease | 10 s |

## Logging

One JSON line per step, same fields as 007 (`request_id`, `service`, `step`, `tx`, `outcome`,
`elapsed_ms`). New `step` values: `publish` (with `topic`, `type`), `consume` (with `topic`,
`partition`, `offset`), `await` (outcome `ok`, `timeout`, `replayed`). New `outcome` values:
`republished`, `ignored-stale`, `dead-lettered`. Checkout and Inventory both log lines for the same
request id because the id travels in the event.
