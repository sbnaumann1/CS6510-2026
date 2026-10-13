# Data Model: Service-Based Architecture (005)

**Feature**: `005-service-architecture` | **Date**: 2026-10-13

The **schema is unchanged**. Every table, column, index and constraint is exactly as defined
in [001 data-model.md](../001-checkout-backend/data-model.md), and the external contract is
unchanged. What this feature adds is **ownership**: which service may write each table,
which may read it, and which repository module holds each statement.

## 1. Table ownership registry

This is the source of truth for the boundary check (research R12). It lives in code as
`checkout_data.ownership.TABLES`, and this table must match it.

| Table | Owner (sole writer at runtime) | Readers | Written by | Read for |
| --- | --- | --- | --- | --- |
| `catalog_item` | catalog | catalog, checkout, inventory, analytics | seed tool only | `/items` body; scan pricing; receipt lines; low-stock names; ranking names |
| `inventory_stock` | checkout | checkout, inventory | completion decrement; seed | stock locks; low-stock report |
| `transaction` | checkout | checkout | start, scan counters, complete, sweep | everything in `/transactions…` |
| `transaction_item` | checkout | checkout, analytics | scan insert | basket at completion; **global scan sequence** for analytics |
| `low_stock_alert` | checkout | inventory | alert emission at completion | `triggeredAt` in the low-stock report |
| `popular_window_snapshot` | analytics | analytics | persistent pipeline `OutputFilter` | popular-items read path |

Notes:
- `catalog_item` has an owner (Catalog) but no runtime writer. It is immutable during a run (001 assumption), so the services' independent caches of it cannot diverge.
- The seed tool (`checkout_data.seed`) and the invariant check (`checkout_data.verify_invariant`) are operator tools, not services. They are exempt from ownership: the seed tool owns the schema itself.
- Cross-service data flow happens only along the Readers column. Checkout → Analytics goes through `transaction_item`. Checkout → Inventory goes through `inventory_stock` and `low_stock_alert`. The catalog flows to everyone through `catalog_item`.

## 2. Repository modules (shared library `checkout_data.repos`)

Every function keeps 002's contract: one statement against the caller's session; it never commits, rolls back or begins.

### `repos/catalog.py`: read-only, importable by every service

| Function | Moved from | Tables (R = read) |
| --- | --- | --- |
| `load_catalog(session) -> list[Row(sku, name, price_cents)]` | the `select` in `app/catalog_cache.load` | `catalog_item` R |

`checkout_data.catalog.CatalogCache` wraps it with the existing in-memory API: `load`, `get(sku)`, `name_of(sku)`, `size`. The pre-rendered `/items` body moves to `catalog_service`, since only Catalog needs it.

### `repos/checkout.py`: imported only by `checkout_service`

| Function | Moved from `app/db/transactions_repo.py` | Tables (R/W) |
| --- | --- | --- |
| `insert_transaction` | same | `transaction` W |
| `scan_item` (CTE) | same | `transaction` W, `transaction_item` W |
| `get_status` | same | `transaction` R |
| `lock_transaction` | same | `transaction` R (FOR UPDATE) |
| `get_basket` | same | `transaction_item` R |
| `lock_stock` | same | `inventory_stock` R (FOR UPDATE) |
| `decrement_stock` | same | `inventory_stock` W |
| `complete_transaction` | same | `transaction` W |
| `get_transaction` | same | `transaction` R |
| `insert_alerts` | same | `low_stock_alert` W |
| `sweep_abandoned` | same | `transaction` W |

### `repos/inventory.py`: imported only by `inventory_service`

| Function | Moved from `app/db/analytics_repo.py` | Tables |
| --- | --- | --- |
| `low_stock_report(session, threshold)` | same (`_LOW_STOCK`) | `inventory_stock` R, `catalog_item` R, `low_stock_alert` R |
| `now(session)` | same (duplicated, a one-line `SELECT now()`) | none |

### `repos/analytics.py`: imported only by `analytics_service`

| Function | Moved from `app/db/analytics_repo.py` | Tables |
| --- | --- | --- |
| `max_scan_seq` | same (used once, at pipeline bootstrap) | `transaction_item` R |
| `get_scans_after(session, last_id, limit=1000)` | same; the default batch rises from 100 to 1 000 for the tail | `transaction_item` R |
| `window_counts` | same (on-demand read path below the first boundary, and the test oracle for FR-012b) | `transaction_item` R |
| `upsert_snapshot` | same | `popular_window_snapshot` W |
| `read_snapshot` | same | `popular_window_snapshot` R |
| `now(session)` | same | none |

### Shared primitives (`checkout_data/__init__` / `db.py`)

`engine`, `SessionLocal`, `get_session`, `advisory_lock`, `dispose_engine` and `POPULAR_RECOMPUTE_LOCK` are unchanged from `app/db/__init__.py`. The lock now marks the **pipeline leader** rather than a single recompute. Pool size comes from each service's own `DB_POOL_SIZE`.

## 3. Persistent pipeline state (in memory, Analytics leader only)

Design: [research.md R5–R6](./research.md). The state lives across windows for the whole
life of the pipeline.

### Items flowing between filters

| Item | Producer → consumer | Fields |
| --- | --- | --- |
| `ScanData` | TailIngest → WindowFilter | `scan_id: int`, `sku: str` (unchanged from 004) |
| `Watermark` | TailIngest → WindowFilter | `boundary: int`, a multiple of `slide`: every id ≤ boundary has arrived or been declared a gap |
| `Slide` | WindowFilter → AggregationFilter | `window_start`, `window_end`, `added: list[str]`, `evicted: list[str]` (SKUs) |
| `WindowCounts` | AggregationFilter → RankingFilter | `window_start`, `window_end`, `counts: dict[str, int]` (a snapshot copy) |
| `RankedData` | RankingFilter → OutputFilter | unchanged from 004; ranking ordered by `(−scanCount, sku)`, top 200 |
| `TERMINATE` | lifespan → TailIngest → … | shutdown only, never between windows |

### Per-filter state

| Filter | State | Initial value (bootstrap) | Invariant |
| --- | --- | --- | --- |
| TailIngest | `last_id` (contiguous watermark), `pending: dict[id, sku]`, `hole_since: float \| None`, `next_boundary` | `last_id = max(0, b₀ − window)` where `b₀ = max_seq − max_seq mod slide`; `next_boundary = last_id + slide` rounded up to a multiple of `slide` | every id ≤ `last_id` has been emitted exactly once or declared a gap |
| WindowFilter | `buffer: deque[ScanData]` | empty | holds exactly the arrived ids in `(last_boundary − window, last_id]` |
| AggregationFilter | `counts: Counter[str]` | empty | equals `Counter(s.sku for s in buffer if id in (b − window, b])` after each `Slide(b)` |
| RankingFilter | none | — | — |
| OutputFilter | `slot` (latest-wins, size 1), `last_written_end` | `None`, 0 | writes are monotone in `window_end` |

### Lifecycle

`standby` (no leader lock) → `bootstrapping` (backfill from `b₀ − window`) → `streaming`
(live tail). There are two other transitions:
- **Error**: any filter exception goes to `restarting`, where all filters are cancelled, then a 1 s back-off, then `bootstrapping`.
- **Shutdown**: `TERMINATE` drains the filters, then `stopped`.

There is no per-slide state transition: windows are emitted while in `streaming`. No state
requires Checkout.

Each persistent filter must remain unit-testable with mock queues, as in 004 SC3.

## 4. Configuration per service (environment)

The variables from 001 are unchanged. The new ones are marked ★.

| Variable | catalog | checkout | inventory | analytics | seed/tools | Default |
| --- | --- | --- | --- | --- | --- | --- |
| `DATABASE_URL` | ✓ | ✓ | ✓ | ✓ | ✓ | `postgresql+asyncpg://checkout:checkout@postgres:5432/checkout` (compose) |
| `WORKERS` | 2 | 6 | 1 | 1 | — | per service |
| `DB_POOL_SIZE` | 2 | 10 | 4 | 4 | — | per service |
| `CATALOG_SIZE` / `STOCK_PER_ITEM` | | | | | ✓ | 2000 / 10000 |
| `LOW_STOCK_THRESHOLD` | | ✓ | ✓ | | | 50 |
| `TX_ABANDON_MINUTES` | | ✓ | | | | 5 |
| `POPULAR_WINDOW_SIZE` / `POPULAR_SLIDE_INTERVAL` | | | | ✓ | | 1000 / 500 |
| ★ `POPULAR_POLL_MS` (tail idle sleep) | | | | ✓ | | 100 |
| ★ `POPULAR_GAP_GRACE_MS` (hole wait before skipping) | | | | ✓ | | 1000 |
| ★ `SERVICE_NAME` | ✓ | ✓ | ✓ | ✓ | | baked into each image; used by `/healthz` and logs |

No service receives another service's hostname or port (R12, rule 6).
