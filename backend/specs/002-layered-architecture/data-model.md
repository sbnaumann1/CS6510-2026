# Phase 1 Data Model: Layered Architecture Refactor

**Feature**: `002-layered-architecture` | **Date**: 2026-09-23 | **Source**: [spec.md](./spec.md) Key Entities, resolved by [research.md](./research.md)

This feature does not change the database schema — `app/models.py` and the six tables it defines
(`catalog_item`, `inventory_stock`, `transaction`, `transaction_item`, `low_stock_alert`,
`popular_window_snapshot`) are unchanged from `specs/001-checkout-backend/data-model.md` (spec
FR-008). What this feature adds is a new internal contract: the set of functions the
database-access layer exposes to the two business layers. That contract is documented here in
place of a schema, since it is the thing this refactor actually introduces.

Every function below takes an `AsyncSession` as its first argument, executes exactly one
statement (or, for `lock_stock`, one statement whose result is discarded), and does not call
`commit`, `rollback`, or `begin` — per research.md R4, transaction-boundary control stays with the
caller.

## `app/db/transactions_repo.py` — consumed only by `app/transactions/*`

| Function | Params | Returns | Replaces (today) |
| --- | --- | --- | --- |
| `insert_transaction` | `station_id: str` | row: `id, status, item_count, running_total_cents, started_at` | `services/transactions.py` `_INSERT_TX` |
| `scan_item` | `tx_id: int, sku: str, price_cents: int` | row or `None`: `item_count, running_total_cents, scan_seq` | `_SCAN` |
| `get_status` | `tx_id: int` | `str \| None` | `_TX_STATUS` |
| `lock_transaction` | `tx_id: int` | row or `None`: `status, station_id, started_at` | `_LOCK_TX` |
| `get_basket` | `tx_id: int` | `list[row: sku, qty]` | `_BASKET` |
| `lock_stock` | `skus: list[str]` | `None` (locks held for the caller's transaction) | `_LOCK_STOCK` |
| `decrement_stock` | `basket: list[tuple[sku, qty]]` | `list[row: sku, current_stock]` (short list ⇒ insufficient stock) | inline `UPDATE ... FROM (VALUES ...)` |
| `complete_transaction` | `tx_id: int, total_cents: int` | `datetime` (`completed_at`) | `_COMPLETE_TX` |
| `get_transaction` | `tx_id: int` | row or `None`: `id, station_id, status, item_count, running_total_cents, started_at` | `_GET_TX` |
| `insert_alerts` | `crossings: list[dict[sku, current_stock, threshold]]` | `None` | `services/inventory.py` `_INSERT_ALERT` |
| `sweep_abandoned` | `minutes: int` | `int` (rowcount cancelled) | `background.py` `sweep_abandoned`'s inline `UPDATE` |

## `app/db/analytics_repo.py` — consumed only by `app/analytics/*`

| Function | Params | Returns | Replaces (today) |
| --- | --- | --- | --- |
| `max_scan_seq` | — | `int` | `services/analytics.py` `_MAX_SEQ` |
| `window_counts` | `start: int, end: int, depth: int` | `list[row: sku, scan_count]` | `_WINDOW_COUNTS` |
| `upsert_snapshot` | `window_size, slide_interval, start, end, ranking_json: str` | `None` | `_UPSERT` |
| `read_snapshot` | — | row or `None`: `window_size, slide_interval, window_start, window_end, computed_at, ranking` | `_READ` |
| `low_stock_report` | `threshold: int` | `list[row: sku, name, current_stock, triggered_at]` | `services/inventory.py` `_LOW_STOCK` |
| `now` | — | `datetime` | `_NOW` (used by both the low-stock `generatedAt` field and, previously, transactions) |

## Business-layer call graph (who calls what, post-refactor)

| Business function | Layer | Calls (db layer) | Calls (shared infra) |
| --- | --- | --- | --- |
| `transactions.service.start_transaction` | Transactions | `transactions_repo.insert_transaction` | — |
| `transactions.service.scan_item` | Transactions | `transactions_repo.scan_item`, `.get_status` | `catalog_cache.get` |
| `transactions.service.complete_transaction` | Transactions | `transactions_repo.lock_transaction`, `.get_basket`, `.lock_stock`, `.decrement_stock`, `.complete_transaction` | `catalog_cache.get`, `transactions.alerts.emit_crossings` |
| `transactions.alerts.emit_crossings` | Transactions | `transactions_repo.insert_alerts` | — |
| `transactions.sweeper.sweep_abandoned` | Transactions | `transactions_repo.sweep_abandoned` | — |
| `analytics.popular_items.recompute` | Analytics | `analytics_repo.max_scan_seq`, `.window_counts`, `.upsert_snapshot` | — |
| `analytics.popular_items.read` | Analytics | `analytics_repo.read_snapshot`, `.max_scan_seq`, `.window_counts` | `catalog_cache.name_of` |
| `analytics.low_stock.read` | Analytics | `analytics_repo.low_stock_report`, `.now` | — |

No row in this table crosses from a Transactions-layer function to an Analytics-layer repo
function or vice versa — that is spec FR-006 made concrete.

## Unchanged reference

Physical schema (DDL, indexes, invariants, row-count expectations) is unchanged from
[`specs/001-checkout-backend/data-model.md`](../001-checkout-backend/data-model.md); this document
does not repeat it.
