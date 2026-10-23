# Contract: Completion Saga

Orchestrator: **Checkout** (`saga.py`). Participant: **Inventory**. Public entry point:
`POST /transactions/{id}/complete`. Data and states: [data-model.md](../data-model.md).
Wire calls: [internal-apis.md](./internal-apis.md).

Each attempt to complete a transaction has an **epoch** (1 for the first, then one more each time
a rolled-back transaction is completed again). Reservations in Inventory are keyed by
`(transaction, epoch)`, so a release of one attempt can never block the next.

## Happy path

```text
Shopper        Gateway       Checkout (checkout-db)               Inventory (inventory-db)
  │ POST complete │               │                                      │
  │──────────────▶│──────────────▶│ 1. LOCAL TX                          │
  │               │               │    lock tx row, require OPEN         │
  │               │               │    basket empty? -> 409 EMPTY_BASKET │
  │               │               │    tx -> COMPLETING                  │
  │               │               │    completion(RESERVING, epoch n)    │
  │               │               │    COMMIT                            │
  │               │               │ 2. POST /internal/reservations ─────▶│ LOCAL TX, ONE STATEMENT
  │               │               │    (tx, epoch n, lines)              │   already recorded? replay
  │               │               │                                      │   lock stock rows (SKU order)
  │               │               │                                      │   check, decrement, alerts,
  │               │               │                                      │   adjustment(APPLIED)  COMMIT
  │               │               │◀──────────────── 200 APPLIED ────────│   (hot lock released here)
  │               │               │    completion -> RESERVED            │
  │               │               │ 3. LOCAL TX                          │
  │               │               │    tx -> COMPLETED, completed_at     │
  │               │               │    completion -> COMPLETED  COMMIT   │
  │◀──────────────│◀──────────────│ 200 Receipt                          │
```

Pre-saga checks keep the 005 error precedence: unknown transaction `404`, not open `409
TRANSACTION_NOT_OPEN`, empty basket `409 EMPTY_BASKET`. These run in step 1 before any state
changes.

## Outcomes and compensation

| # | Situation | Compensation | Final transaction state | Shopper sees |
| --- | --- | --- | --- | --- |
| A | Step 2 returns `409 INSUFFICIENT_STOCK` | None (Inventory changed nothing). Completion record closed `ABORTED` | `OPEN` | `409 INSUFFICIENT_STOCK` (message from `shortSkus`) |
| B | Step 2 times out or errors | Retry `reserve` (2 more tries, 4 s total). If still unknown: `COMPENSATING`, `release` (which leaves a record), reopen, `ABORTED` | `OPEN` | `503 INTERNAL_ERROR` |
| C | Step 2 returns `RESERVATION_RELEASED` | Reopen, `ABORTED` (no release needed, one is already recorded) | `OPEN` | `503 INTERNAL_ERROR` |
| D | Step 3 fails after reserve succeeded | `COMPENSATING`, `release`, reopen, `ABORTED` | `OPEN` | `503 INTERNAL_ERROR` |
| E | `release` cannot be delivered (Inventory down) | Stay `COMPENSATING`. The shopper's next `complete` finishes it (release, reopen) and then starts a fresh attempt; failing that the recovery loop does | `COMPLETING` (reported as `OPEN`) until delivered | `503 INTERNAL_ERROR`, then the retry succeeds once Inventory is back |
| F | Checkout crashes in state `RESERVING` | Recovery: `release`, reopen | `OPEN` | (connection lost) retry |
| G | Checkout crashes in state `RESERVED` | Recovery: finalize (roll forward) | `COMPLETED` | (connection lost) `GET` shows `COMPLETED` |
| H | Inventory crashes mid-statement | Its local transaction rolls back. Checkout sees B | `OPEN` | `503` |

## Idempotency rules

1. **Key**: the transaction id **and the epoch**, in the reserve and release calls and as the
   adjustment's primary key.
2. **Reserve** applies at most once per `(transaction, epoch)`. A repeat is answered from the
   recorded result inside the same statement; if two identical reserves race, the loser's insert
   conflicts and it is repeated in a new transaction (`reserve_once`).
3. **Release** reverses at most once. Repeats, and releases of attempts that never applied, are
   harmless, and a release always leaves a record so a late reserve **of the same epoch** is
   refused (`RESERVATION_RELEASED`). The next epoch is not blocked.
4. **Finalize** is guarded by `status = 'COMPLETING'`; a second driver finds the work done.
5. **A second `complete` while a live driver is working** (its completion record touched within
   the lease, 10 s) is refused with `409 TRANSACTION_NOT_OPEN`, exactly as 005 refused the second
   of two overlapping completes (INV-3). Once the lease has expired, a retry **takes over** the
   saga from its recorded state, using only idempotent steps.
6. A `complete` on an already `COMPLETED` or `CANCELLED` transaction returns the contract's
   `409 TRANSACTION_NOT_OPEN`.
7. A scan on a `COMPLETING` transaction returns `409 TRANSACTION_NOT_OPEN` (the scan statement
   requires `OPEN`).
8. A `complete` that finds a `COMPENSATING` record (row E) first finishes that rollback; if the
   release is still undeliverable it answers `503` and changes nothing.

## Recovery loop

Every 2 s, each Checkout instance claims, per candidate (`FOR UPDATE SKIP LOCKED`,
`updated_at` older than the 10 s lease, state not `COMPLETED` or `ABORTED`) by bumping the lease,
then acts outside that transaction:

| State | Action |
| --- | --- |
| `RESERVING` | `release` (outcome unknown), then reopen, `ABORTED` |
| `RESERVED` | Step 3 |
| `COMPENSATING` | `release`, then reopen, `ABORTED` |

Each claim bumps `attempts` and `updated_at`, and the action logs `step`, `tx`, `outcome`.

## Timing budget (FR-017)

| Step | Limit |
| --- | --- |
| Step 1 / 3 local transactions | database statement timeout (5 s) |
| Each `reserve` / `release` attempt | 2 s read timeout, 0.5 s connect |
| Whole synchronous saga | ≤ 4 s for reserve, plus one `release` attempt (2 s): worst case about 6 s before `503` |
| Recovery lease | 10 s |

## Logging

One JSON line per step: `request_id`, `service`, `step` (`begin`, `reserve`, `finalize`,
`release`, `recover`, and `call <peer>` for failed outbound calls), `tx`, `outcome` (`ok`,
`took-over`, `rejected`, `released`, `replayed`, `timeout`, `compensated`, `undelivered`),
`elapsed_ms`. Both Checkout and Inventory log a `reserve` line for the same request id.
