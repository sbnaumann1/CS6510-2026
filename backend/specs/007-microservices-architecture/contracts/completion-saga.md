# Contract: Completion Saga

Orchestrator: **Checkout** (`saga.py`). Participant: **Inventory**. Public entry point:
`POST /transactions/{id}/complete`. Data and states: [data-model.md](../data-model.md).
Wire calls: [internal-apis.md](./internal-apis.md).

## Happy path

```text
Shopper        Gateway       Checkout (checkout-db)               Inventory (inventory-db)
  │ POST complete │               │                                      │
  │──────────────▶│──────────────▶│ 1. LOCAL TX                          │
  │               │               │    lock tx row, require OPEN         │
  │               │               │    basket empty? -> 409 EMPTY_BASKET │
  │               │               │    tx -> COMPLETING                  │
  │               │               │    completion(RESERVING, total)      │
  │               │               │    COMMIT                            │
  │               │               │ 2. POST /internal/reservations ─────▶│ LOCAL TX
  │               │               │                                      │   replay check
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
changes. A transaction already `COMPLETING` is treated as in flight, not as an error: the call
joins the saga (below).

## Outcomes and compensation

| # | Situation | Compensation | Final transaction state | Shopper sees |
| --- | --- | --- | --- | --- |
| A | Step 2 returns `409 INSUFFICIENT_STOCK` | None (Inventory changed nothing). Delete completion row | `OPEN` | `409 INSUFFICIENT_STOCK` (message from `shortSkus`) |
| B | Step 2 times out or errors | Retry `reserve` (2 more tries, 4 s total). If still unknown: `COMPENSATING`, `release`, delete completion row | `OPEN` | `503 INTERNAL_ERROR` |
| C | Step 2 returns `RESERVATION_RELEASED` | As B (no `release` needed, one is already recorded) | `OPEN` | `503 INTERNAL_ERROR` |
| D | Step 3 fails after reserve succeeded | `COMPENSATING`, `release`, delete completion row | `OPEN` | `503 INTERNAL_ERROR` |
| E | `release` cannot be delivered | Stay `COMPENSATING`; recovery retries | `COMPLETING` until delivered, then `OPEN` | `503 INTERNAL_ERROR` already returned |
| F | Checkout crashes in state `RESERVING` | Recovery: `release`, then `OPEN` | `OPEN` | (connection lost) retry |
| G | Checkout crashes in state `RESERVED` | Recovery: finalize (roll forward) | `COMPLETED` | (connection lost) `GET` shows `COMPLETED` |
| H | Inventory crashes mid-statement | Its local transaction rolls back. Checkout sees B | `OPEN` | `503` |

## Idempotency rules

1. **Key**: the transaction id, in the reserve and release calls and as `adjustment` primary key.
2. **Reserve** applies at most once. Repeats return the recorded result.
3. **Release** reverses at most once. Repeats and releases of never-applied reservations are
   harmless, and a release always leaves a record so a late reserve cannot apply afterwards.
4. **Finalize** is guarded by `status = 'COMPLETING'`; a second driver finds the work done.
5. **Concurrent `complete` calls** on one transaction: the second sees `COMPLETING` and re-drives
   the same steps. The idempotent calls guarantee one stock decrement and one `COMPLETED`.
6. A `complete` on an already `COMPLETED` or `CANCELLED` transaction returns the contract's `409
   TRANSACTION_NOT_OPEN`.
7. A scan on a `COMPLETING` transaction returns `409 TRANSACTION_NOT_OPEN` (the scan statement
   requires `OPEN`).

## Recovery loop

Every 2 s, each Checkout instance runs, per candidate (`FOR UPDATE SKIP LOCKED`, `updated_at`
older than 10 s, state not `COMPLETED`):

| State | Action |
| --- | --- |
| `RESERVING` | `release` (outcome unknown), then transaction → `OPEN`, delete row |
| `RESERVED` | Step 3 |
| `COMPENSATING` | `release`, then transaction → `OPEN`, delete row |

Each action bumps `attempts` and `updated_at`, and logs `step`, `tx`, `outcome`.

## Timing budget (FR-017)

| Step | Limit |
| --- | --- |
| Step 1 / 3 local transactions | database statement timeout (5 s) |
| Each `reserve` / `release` attempt | 2 s read timeout, 0.5 s connect |
| Whole synchronous saga | ≤ 4 s for reserve, plus one `release` attempt (2 s): worst case about 6 s before `503` |
| Recovery lease | 10 s |

## Logging

One JSON line per step: `request_id`, `service`, `step` (`begin`, `reserve`, `finalize`,
`release`, `recover`), `tx`, `outcome` (`ok`, `rejected`, `retry`, `timeout`, `compensated`),
`elapsed_ms`.
