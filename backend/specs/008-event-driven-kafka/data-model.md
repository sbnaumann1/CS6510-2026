# Data Model: Event-Driven Microservice Communication

What is new or changed relative to [spec 007's data model](../007-microservices-architecture/data-model.md).
Everything not listed is unchanged. Event payloads are in [contracts/events.md](./contracts/events.md).

## Ownership

| Data | Owner | Where it lives | Who else holds a copy |
| --- | --- | --- | --- |
| Item name and price | Catalog | read-only `catalog.csv` | Snapshots in Checkout, Inventory, Analytics (from `catalog.items.v1`) |
| Transactions, scans, completions | Checkout | `checkout-db` | Scans also in `checkout.scans.v1` (Analytics' source) |
| Stock, adjustments, low-stock alerts | Inventory | `inventory-db` | Outcomes in `inventory.stock.v1` (Checkout's replies) |
| Popular-items window | Analytics | memory | none; rebuilt from `checkout.scans.v1` |
| Event history | Kafka | broker log | none |

A **snapshot** is a read-only, in-memory copy with a documented owner and source topic (FR-010):

| Snapshot | Holder | Source topic | Content | Ready when |
| --- | --- | --- | --- | --- |
| Item prices and names | Checkout | `catalog.items.v1` | SKU → (name, price in cents) | marker seen and item count matches |
| Item names | Inventory | `catalog.items.v1` | SKU → name | same |
| Item names | Analytics | `catalog.items.v1` | SKU → name | same |

## Checkout (`checkout-db`)

### New table: `outbox`

Completion-request events awaiting (or already given) publication.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | bigint identity PK | publication order |
| `topic` | text | `checkout.completions.v1` |
| `key` | text | transaction id, e.g. `tx-17` |
| `event_id` | uuid | unique; the envelope's `eventId` |
| `payload` | jsonb | the full envelope |
| `created_at` | timestamptz | |
| `published_at` | timestamptz null | set after the broker acknowledges |

Index on `(id) WHERE published_at IS NULL` for the sweeper. Rows are written in the **same local
transaction** as the `completion` change that causes them. Published rows older than 1 hour are
deleted.

### New table: `relay_state`

One row: `last_published_scan_id bigint`. Updated after Kafka acknowledges a batch of scans.
Guarded by a PostgreSQL advisory lock so one worker relays at a time.

### Changed: `scan` (no schema change)

The existing `scan` table is the outbox for scans. The relay selects `id > last_published_scan_id`
in order and publishes the contiguous prefix. An id that is missing is a gap until the grace period
(1 s) has passed, then a permanent gap and is skipped.

### Changed: `scan_log`

Kept for the operator check only (token naming this incarnation of the log). If the log is reset,
the relay resets `last_published_scan_id` and publishes a `scan-log-reset` marker so Analytics can
discard its window ([events.md](./contracts/events.md)).

### Unchanged: `completion`

States, epoch, lease and the recovery rules are those of 007's saga
([completion-flow.md](./contracts/completion-flow.md)). The only difference is that stock steps
publish an event and await a reply instead of making an HTTP call.

## Inventory (`inventory-db`)

### Changed: `stock_adjustment.state`

Adds **`REJECTED`**. Constraint becomes `state IN ('APPLIED', 'RELEASED', 'REJECTED')`.
`REJECTED` records "this `(transaction, epoch)` was refused for insufficient stock" and the
`shortSkus`, changes no stock, and makes a repeated request replay the same refusal. The
invariant checker counts only `APPLIED` rows, as before.

### New table: `outbox`

Same shape as Checkout's, `topic = inventory.stock.v1`. Written in the same local transaction as
the adjustment (or as the replay of one) and published after COMMIT.

### No `processed_event` table

The "processed-event record" of the spec is the existing per-`(transaction, epoch)` adjustment row
(now including `REJECTED`): it is written in the same local transaction as the stock effect, so it
is the idempotency key. A second table would only duplicate it and add a write per request.

## Analytics (memory only)

| State | Source | Rule |
| --- | --- | --- |
| Rolling window of the last `POPULAR_WINDOW_SIZE` scans | `checkout.scans.v1` | applied in `scanId` order; a `scanId` at or below the highest applied is ignored (duplicate) |
| Item names | catalog snapshot | falls back to the SKU if a name is absent |
| Consumed position | consumer group `analytics` | committed after the window update |

After a restart Analytics seeks to `end − 2 × window_size` and reads forward; it needs nothing from
Checkout.

## Events (summary)

| Event type | Topic | Produced by | Consumed by | Key |
| --- | --- | --- | --- | --- |
| `catalog.item-published` | `catalog.items.v1` | Catalog | Checkout, Inventory, Analytics | SKU |
| `catalog.catalog-published` | `catalog.items.v1` | Catalog | Checkout, Inventory, Analytics | `__marker__` |
| `checkout.scan-recorded` | `checkout.scans.v1` | Checkout | Analytics | none |
| `checkout.scan-log-reset` | `checkout.scans.v1` | Checkout | Analytics | none |
| `checkout.stock-reservation-requested` | `checkout.completions.v1` | Checkout | Inventory | transaction id |
| `checkout.stock-release-requested` | `checkout.completions.v1` | Checkout | Inventory | transaction id |
| `inventory.stock-reserved` | `inventory.stock.v1` | Inventory | Checkout | transaction id |
| `inventory.stock-rejected` | `inventory.stock.v1` | Inventory | Checkout | transaction id |
| `inventory.stock-released` | `inventory.stock.v1` | Inventory | Checkout | transaction id |
| `inventory.reservation-refused` | `inventory.stock.v1` | Inventory | Checkout | transaction id |
| `dead-letter` | `dead-letters.v1` | any consumer | operators | source topic |

## State transitions

Transaction and completion states are unchanged from 007:

```text
Transaction:  OPEN ──complete──▶ COMPLETING ──finalize──▶ COMPLETED
                ▲                    │
                └──── reopen ◀───────┘   (rejected, rolled back, or recovered)

Completion:   RESERVING ──stock-reserved──▶ RESERVED ──finalize──▶ COMPLETED
                  │                             │
                  └─ rejected ─▶ ABORTED        └─ finalize fails ─▶ COMPENSATING ─▶ ABORTED
                  └─ no outcome in time ──────────────────────────▶ COMPENSATING ─▶ ABORTED
```

Inventory's adjustment per `(transaction, epoch)`:

```text
(none) ──reserve ok──▶ APPLIED ──release──▶ RELEASED
(none) ──reserve short──▶ REJECTED
(none) ──release first──▶ RELEASED          (a later reserve of that epoch is refused)
```

## Validation rules (from the requirements)

- An event is applied at most once in effect per consumer (FR-011): by `(transaction, epoch)` in
  Inventory, by completion state and epoch in Checkout, by `scanId` in Analytics, by SKU overwrite
  in snapshots.
- A state change and its event are committed together (FR-012): outbox rows are written in the
  state-changing transaction; the scan table is its own outbox.
- Stock is never negative and sold equals scanned in completed transactions (FR-013): unchanged
  conditional decrement inside one statement.
- An outbox row with `published_at IS NULL` older than 2 s is a defect the checker reports on a
  quiescent system (FR-021).
