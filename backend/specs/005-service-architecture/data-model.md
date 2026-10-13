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
| `popular_window_snapshot` | analytics | analytics | pipeline `OutputFilter` | popular-items read path; poller's last-published boundary |

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
| `max_scan_seq` | same | `transaction_item` R |
| `get_scans_after` | same | `transaction_item` R |
| `window_counts` | same | `transaction_item` R |
| `upsert_snapshot` | same | `popular_window_snapshot` W |
| `read_snapshot` | same | `popular_window_snapshot` R |
| `last_published_end(session) -> int` | **new**, `SELECT coalesce(max(window_end),0) FROM popular_window_snapshot` | `popular_window_snapshot` R |
| `now(session)` | same | none |

### Shared primitives (`checkout_data/__init__` / `db.py`)

`engine`, `SessionLocal`, `get_session`, `advisory_lock`, `dispose_engine` and `POPULAR_RECOMPUTE_LOCK` are unchanged from `app/db/__init__.py`. Pool size comes from each service's own `DB_POOL_SIZE`.

## 3. Analytics poller state (in memory, per Analytics process)

| Field | Type | Initial value | Meaning |
| --- | --- | --- | --- |
| `last_end` | int | `last_published_end()` at startup | `window_end` of the most recent snapshot this process knows is published |
| `poll_interval` | seconds | `POPULAR_POLL_MS / 1000` (0.25) | sleep between ticks |

Per tick:
1. `max_seq ← max_scan_seq()`.
2. `b ← max_seq − (max_seq mod slide)`.
3. If `b ≥ slide` and `b > last_end`, try the advisory lock. If it's acquired, run the pipeline for `(max(0, b − window), b]`, then `last_end ← b`. If it's held elsewhere, skip; the next tick re-reads.
4. If `b ≤ last_end`, refresh `last_end` from the snapshot every 20 ticks. This covers another replica having published.

State transitions: `starting → polling ⇄ computing → polling`. A failure in `computing` is logged and returns to `polling`, and `last_end` doesn't advance (004 FR11). There is no state that requires Checkout.

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
| ★ `POPULAR_POLL_MS` | | | | ✓ | | 250 |
| ★ `SERVICE_NAME` | ✓ | ✓ | ✓ | ✓ | | baked into each image; used by `/healthz` and logs |

No service receives another service's hostname or port (R12, rule 6).
