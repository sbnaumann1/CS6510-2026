# Data Model: Microservices Architecture

Four independent schemas, one per service, each in its own PostgreSQL instance. **No foreign keys
cross a service boundary** and no table is readable by another service. Money is integer cents,
timestamps are `TIMESTAMPTZ` (unchanged from 001).

## 1. Catalog (`catalog-db`)

| Table | Columns | Notes |
| --- | --- | --- |
| `catalog_item` | `sku` text PK, `name` text, `price_cents` int `> 0` | Seeded once, immutable during a run. Served by `GET /items` |

## 2. Checkout (`checkout-db`)

| Table | Columns | Notes |
| --- | --- | --- |
| `transaction` | `id` bigint identity PK, `station_id` text, `status` enum `OPEN/COMPLETING/COMPLETED/CANCELLED`, `item_count` int, `running_total_cents` bigint, `total_amount_cents` bigint null, `started_at`, `completed_at` null | `COMPLETING` is internal; the public API reports it as `OPEN`. Partial index on `started_at WHERE status='OPEN'` for the sweeper |
| `transaction_item` | `id` bigint identity PK (the global **scan sequence**), `transaction_id` bigint FK → `transaction`, `sku` text, `unit_price_cents` int, `scanned_at` | `sku` has **no** FK (catalog is another service's data); `unit_price_cents` is the price snapshot taken at scan time |
| `completion` | `transaction_id` bigint PK FK → `transaction`, `state` enum, `total_cents` bigint, `attempts` int, `updated_at` timestamptz, `last_error` text null | One row per in-flight or finished saga. Drives recovery. See state machine below |
| `relay_cursor` | `id` smallint PK (=1), `last_scan_id` bigint, `updated_at` | The scan relay's durable position |

**Transaction state machine**

```text
OPEN ──complete──▶ COMPLETING ──finalize──▶ COMPLETED
  ▲                    │
  └─ reserve rejected / saga compensated
OPEN ──sweeper (stale)──▶ CANCELLED          (COMPLETING rows are never swept)
```

**Completion state machine**

```text
RESERVING ──reserve ok──▶ RESERVED ──finalize──▶ COMPLETED
    │                         │
    │ rejected: row deleted   │ finalize fails
    │ unknown outcome         ▼
    └────────────────▶ COMPENSATING ──release acked──▶ (row deleted, tx → OPEN)
```

Rules: `COMPLETED` rows are kept for audit; `RESERVING/RESERVED/COMPENSATING` rows older than the
recovery lease are picked up by the recovery loop. A transaction is `COMPLETING` exactly when it
has a non-`COMPLETED` completion row.

## 3. Inventory (`inventory-db`)

| Table | Columns | Notes |
| --- | --- | --- |
| `inventory_stock` | `sku` text PK, `current_stock` int `>= 0`, `initial_stock` int | Hot row for popular SKUs. No FK to catalog |
| `stock_adjustment` | `transaction_id` bigint PK, `state` enum `APPLIED/RELEASED`, `lines` jsonb (`[{sku, qty}]`), `created_at`, `updated_at` | The idempotency record. `RELEASED` with no prior `APPLIED` is a **tombstone** that blocks a late `reserve` |
| `low_stock_alert` | `id` bigint identity PK, `sku` text, `current_stock` int, `threshold` int, `triggered_at` | Raised inside the reserve statement when stock crosses below the threshold (same rule as 001 FR-006). Not deleted on release; it records that stock was once low |

**Adjustment state machine**

```text
(none) ──reserve──▶ APPLIED ──release──▶ RELEASED
(none) ──release──▶ RELEASED (tombstone; later reserve returns "released")
```

## 4. Analytics (`analytics-db`)

| Table | Columns | Notes |
| --- | --- | --- |
| `scan_event` | `scan_id` bigint PK (assigned by Checkout), `sku` text, `received_at` | Written by `POST /internal/scans` with `ON CONFLICT DO NOTHING`. The persistent pipeline tails it by `scan_id` exactly as 005's tailed `transaction_item` |
| `popular_window_snapshot` | unchanged from 005 | Single row, written by the pipeline's output stage |

`scan_event.scan_id` is not an identity column here, so Analytics never invents a sequence. Window
bounds in the public response remain values of Checkout's scan sequence, as before.

## 5. Data crossing a boundary

| Datum | Owner | Consumer | How | Staleness allowed |
| --- | --- | --- | --- | --- |
| Item name, price | Catalog | Checkout (price at scan), Inventory (names in low-stock), Analytics (names in popular-items) | `GET /items`, cached in memory | None: fixed during a run |
| Stock reserve / release | Inventory | Checkout | `POST` / `DELETE /internal/reservations` | None: synchronous |
| Scan `(scan_id, sku)` | Checkout | Analytics | Relay → `POST /internal/scans` | Seconds; catches up after outages |
| Per-SKU sold, stock, scan counts | each owner | the invariant checker | `GET /internal/reports/*` | None on a quiescent system |

## 6. Invariants (checked by `scripts/verify_invariant.py`)

1. Per SKU: `initial_stock − current_stock == completed line items` (Inventory vs Checkout).
2. `current_stock >= 0` everywhere.
3. No transaction is `COMPLETING`, and no `completion` row is in a non-terminal state, on a
   quiescent system.
4. Every `APPLIED` adjustment corresponds to a `COMPLETED` transaction, and every `RELEASED`
   adjustment to a transaction that is not `COMPLETED`.
5. After the relay drains: Analytics `scan_event` count equals Checkout `transaction_item` count.
