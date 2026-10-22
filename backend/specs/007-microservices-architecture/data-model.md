# Data Model: Microservices Architecture

Two independent database schemas (Checkout, Inventory), each in its own PostgreSQL instance, plus Catalog's read-only file and Analytics' in-memory derived state. **No foreign keys
cross a service boundary** and no table is readable by another service. Money is integer cents,
timestamps are `TIMESTAMPTZ` (unchanged from 001).

## 1. Catalog (`catalog.csv`, no database)

One UTF-8 CSV file with header `sku,name,price_cents` and one row per item, in ascending SKU order. Written once by `seed-catalog`, read-only afterwards, loaded into memory by Catalog at startup and served by `GET /items`.

| Column | Type | Rule |
| --- | --- | --- |
| `sku` | text | Unique; the file's order is ascending SKU (the load client derives its popularity rank from response position) |
| `name` | text | Non-empty |
| `price_cents` | int | `> 0` (checked when the file is loaded; a bad row stops startup with a clear error) |

The file lives on a volume mounted read-write into `seed-catalog` and **read-only** into `catalog`, and into nothing else.

## 2. Checkout (`checkout-db`)

| Table | Columns | Notes |
| --- | --- | --- |
| `transaction` | `id` bigint identity PK, `station_id` text, `status` enum `OPEN/COMPLETING/COMPLETED/CANCELLED`, `item_count` int, `running_total_cents` bigint, `total_amount_cents` bigint null, `started_at`, `completed_at` null | `COMPLETING` is internal; the public API reports it as `OPEN`. Partial index on `started_at WHERE status='OPEN'` for the sweeper |
| `transaction_item` | `id` bigint identity PK (the global **scan sequence**), `transaction_id` bigint FK → `transaction`, `sku` text, `unit_price_cents` int, `scanned_at` | Also the durable scan log that Analytics reads through `GET /internal/scans` (primary-key range read). `sku` has **no** FK (catalog is another service's data); `unit_price_cents` is the price snapshot taken at scan time |
| `completion` | `transaction_id` bigint PK FK → `transaction`, `state` enum, `total_cents` bigint, `attempts` int, `updated_at` timestamptz, `last_error` text null | One row per in-flight or finished saga. Drives recovery. See state machine below |

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

## 4. Analytics (in memory, no database)

Nothing is persisted. Everything below is derived from Checkout's scan log and is rebuilt from it on
start ([research R5](./research.md)).

| State | Shape | Notes |
| --- | --- | --- |
| Cursor | `last_scan_id` int | The contiguous watermark of `TailIngest`: every scan id up to it has been read or skipped as a hole that never committed (after `POPULAR_GAP_GRACE_MS`) |
| `ScanWindow` | deque of `(scan_id, sku)`, at most `window_size + slide_interval` entries | Answers the read path before a full window exists (`windowStart` is 0 until then) |
| Aggregation | `sku → count` for the current window | Maintained by `AggregationFilter` as the window slides by `scan_id`, every `slide_interval` scans |
| Ranking | top-200 `(sku, scanCount)`, plus `window_start`, `window_end`, `computed_at` | Latest-wins slot read by `GET /analytics/popular-items` |

`scan_id` is Checkout's sequence, so the public response's `windowStart` and `windowEnd` remain
values of that sequence, as before. There is no table, no snapshot row and no advisory lock.

**Lifecycle**

```text
bootstrapping ──(maxScanId from Checkout; tail from b0 − window)──▶ streaming
streaming ──filter error, or Checkout reports maxScanId < cursor──▶ restarting ──▶ bootstrapping
```

Checkout unreachable: `bootstrapping` retries every 1 s and the endpoint answers with the empty
window; `streaming` retries the poll and keeps serving the last ranking.

## 5. Data crossing a boundary

| Datum | Owner | Consumer | How | Staleness allowed |
| --- | --- | --- | --- | --- |
| Item name, price | Catalog | Checkout (price at scan), Inventory (names in low-stock), Analytics (names in popular-items) | `GET /items`, cached in memory | None: fixed during a run |
| Stock reserve / release | Inventory | Checkout | `POST` / `DELETE /internal/reservations` | None: synchronous |
| Scan `(scan_id, sku)` | Checkout | Analytics | Analytics polls `GET /internal/scans?after=<id>` every `POPULAR_POLL_MS` (100 ms) | Seconds; catches up after outages by rebuilding the last window |
| Per-SKU sold, stock, scan counts | each owner | the invariant checker | `GET /internal/reports/*` | None on a quiescent system |

## 6. Invariants (checked by `scripts/verify_invariant.py`)

1. Per SKU: `initial_stock − current_stock == completed line items` (Inventory vs Checkout).
2. `current_stock >= 0` everywhere.
3. No transaction is `COMPLETING`, and no `completion` row is in a non-terminal state, on a
   quiescent system.
4. Every `APPLIED` adjustment corresponds to a `COMPLETED` transaction, and every `RELEASED`
   adjustment to a transaction that is not `COMPLETED`.
5. Once idle (after the gap grace): Analytics' `lastScanId` equals Checkout's `maxScanId`.
