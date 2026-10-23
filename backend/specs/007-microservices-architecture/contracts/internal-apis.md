# Contract: Internal Service-to-Service APIs

These routes are **not** part of `spec.yaml`. They are served on each service's port 8000, reachable
only on the `mesh` network, and blocked at the gateway (see [gateway-routes.md](./gateway-routes.md)).
All bodies are JSON. Errors use the contract's `ApiError` shape
(`{"error": "<CODE>", "message": "<text>"}`). Every call carries `X-Request-ID`.

Allowed callers (enforced by [service-boundaries.md](./service-boundaries.md)):

| Caller | Callee | Routes |
| --- | --- | --- |
| Checkout, Inventory, Analytics | Catalog | `GET /items` (the public route, reused) |
| Checkout | Inventory | `POST /internal/reservations`, `DELETE /internal/reservations/{transactionId}` |
| Analytics | Checkout | `GET /internal/scans` |
| verification tool | Checkout, Inventory, Analytics | `GET /internal/reports/*` |

## Inventory: reserve stock

`POST /internal/reservations`  (deadline 2 s)

```json
{ "transactionId": "tx-123", "epoch": 1, "lines": [ { "sku": "SKU-000001", "quantity": 2 } ] }
```

`epoch` (integer ≥ 1) names the completion attempt: a reservation is keyed by
`(transactionId, epoch)`. `lines` are unique by `sku`. Order does not matter; Inventory locks in
SKU order.

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | `{ "transactionId": "tx-123", "epoch": 1, "state": "APPLIED", "replayed": false }` | Stock reduced, adjustment recorded, alerts raised. `replayed: true` when the same transaction was already applied (no second decrement) |
| `409` | `{ "error": "INSUFFICIENT_STOCK", "message": "Insufficient stock for SKU-000001.", "shortSkus": ["SKU-000001"] }` | At least one SKU is missing or short. Nothing changed. Checkout re-maps this to its own `409 INSUFFICIENT_STOCK` |
| `409` | `{ "error": "RESERVATION_RELEASED", ... }` | A `release` for this `(transaction, epoch)` already arrived (tombstone). Checkout treats it as a failed attempt and compensates |
| `400` | `ApiError` `INVALID_REQUEST` | `transactionId`, `epoch` or `lines` is missing or malformed |
| `5xx` / timeout | n/a | Unknown outcome. Caller retries the same request (idempotent) |

## Inventory: release (compensation)

`DELETE /internal/reservations/{transactionId}?epoch=<n>`  (deadline 2 s)

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | `{ "transactionId": "tx-123", "epoch": 1, "state": "RELEASED", "unitsRestored": 3 }` | Applied adjustment reversed, or tombstone recorded when there was none (`unitsRestored: 0`) |
| `200` | same, `"alreadyReleased": true` | Idempotent repeat |

## Checkout: read scans (called by Analytics)

`GET /internal/scans?after=<scanId>&limit=<n>`  (deadline 2 s; `limit` 0–1000, default 1000)

A primary-key range read over Checkout's scan table, `id > after ORDER BY id LIMIT n`. Only
committed rows are visible, so a scan id whose transaction has not committed yet is simply absent
and appears on a later call (Analytics' watermark logic copes with the hole).

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | `{ "scans": [ { "scanId": 4511, "sku": "SKU-000002" }, { "scanId": 4512, "sku": "SKU-000007" } ], "maxScanId": 4512, "logId": "9f0c…" }` | Scans after `after`, in id order. `maxScanId` is the highest committed scan id in the whole table (0 when empty), independent of `limit`. `limit=0` returns no scans and is the cheap way to read `maxScanId`. `logId` names this incarnation of the scan log: it changes when Checkout's data is reset, so a reader that sees a different one starts over instead of trusting its position |
| `400` | `ApiError` `INVALID_REQUEST` | `after` or `limit` is missing, not an integer, or out of range |

Reads are idempotent and side-effect free: Checkout keeps no per-caller state (only the one
`logId` row), so Analytics can restart, re-read or re-bootstrap at any time. Overlapping reads
are allowed.

## Read-only reports (verification tool)

| Route | Service | Response |
| --- | --- | --- |
| `GET /internal/reports/sold-by-sku` | Checkout | `{ "sold": { "SKU-000001": 120, ... }, "completing": 0, "openCompletions": 0, "completedTransactions": ["tx-1", ..] }`: units on `COMPLETED` transactions per SKU, count of `COMPLETING` transactions, count of `completion` rows not yet `COMPLETED` or `ABORTED`, ids of completed transactions |
| `GET /internal/reports/scan-count` | Checkout | `{ "scans": 98211, "maxScanId": 98211 }` |
| `GET /internal/reports/cursor` | Analytics | `{ "lastScanId": 98211, "windowEnd": 98000 }`: the tail's watermark and the latest published window end |
| `GET /internal/reports/stock` | Inventory | `{ "stock": [ { "sku": "...", "initial": 10000, "current": 9880 } ], "adjustments": { "applied": ["tx-1", ..], "released": ["tx-2", ..] } }` |

## Client behavior (`checkout_web.client`)

| Setting | Value |
| --- | --- |
| Connect timeout | 0.5 s |
| Read timeout | 2 s (reserve, release, scan reads), 5 s (catalog fetch) |
| Retries | Idempotent calls only: reserve, release, scan reads, catalog GET. Total reserve budget 4 s |
| Keep-alive | One pooled client per target |
| Propagation | Forward `X-Request-ID`; generate one for background work |
| Failure mapping | Exhausted retries raise `UpstreamUnavailable`. On a request path it maps to the contract's `503 INTERNAL_ERROR`. In Analytics' background tail it is logged and retried after a 1 s backoff |
