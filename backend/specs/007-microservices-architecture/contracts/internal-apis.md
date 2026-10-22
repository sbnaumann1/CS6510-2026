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
| Checkout | Analytics | `POST /internal/scans` |
| verification tool | Checkout, Inventory, Analytics | `GET /internal/reports/*` |

## Inventory: reserve stock

`POST /internal/reservations`  (deadline 2 s)

```json
{ "transactionId": "tx-123", "lines": [ { "sku": "SKU-000001", "quantity": 2 } ] }
```

`lines` are unique by `sku`. Order does not matter; Inventory locks in SKU order.

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | `{ "transactionId": "tx-123", "state": "APPLIED", "replayed": false }` | Stock reduced, adjustment recorded, alerts raised. `replayed: true` when the same transaction was already applied (no second decrement) |
| `409` | `{ "error": "INSUFFICIENT_STOCK", "message": "Insufficient stock for SKU-000001.", "shortSkus": ["SKU-000001"] }` | At least one SKU is missing or short. Nothing changed. Checkout re-maps this to its own `409 INSUFFICIENT_STOCK` |
| `409` | `{ "error": "RESERVATION_RELEASED", ... }` | A `release` for this transaction already arrived (tombstone). Checkout treats it as a failed attempt and compensates |
| `5xx` / timeout | n/a | Unknown outcome. Caller retries the same request (idempotent) |

## Inventory: release (compensation)

`DELETE /internal/reservations/{transactionId}`  (deadline 2 s)

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | `{ "transactionId": "tx-123", "state": "RELEASED", "unitsRestored": 3 }` | Applied adjustment reversed, or tombstone recorded when there was none (`unitsRestored: 0`) |
| `200` | same, `"alreadyReleased": true` | Idempotent repeat |

## Analytics: ingest scans

`POST /internal/scans`  (deadline 2 s; batch of up to 1000)

```json
{ "scans": [ { "scanId": 4511, "sku": "SKU-000002" }, { "scanId": 4512, "sku": "SKU-000007" } ] }
```

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | `{ "accepted": 2, "duplicates": 0 }` | Inserted with `ON CONFLICT DO NOTHING`. Replays are safe |

Out-of-order and overlapping batches are allowed. The pipeline's own watermark logic handles gaps.

## Read-only reports (verification tool)

| Route | Service | Response |
| --- | --- | --- |
| `GET /internal/reports/sold-by-sku` | Checkout | `{ "sold": { "SKU-000001": 120, ... }, "completing": 0, "openCompletions": 0, "completedTransactions": [..] }`: units on `COMPLETED` transactions per SKU, count of `COMPLETING` transactions, count of non-terminal `completion` rows, ids of completed transactions |
| `GET /internal/reports/scan-count` | Checkout, Analytics | `{ "scans": 98211, "maxScanId": 98211 }` |
| `GET /internal/reports/stock` | Inventory | `{ "stock": [ { "sku": "...", "initial": 10000, "current": 9880 } ], "adjustments": { "applied": [..tx ids..], "released": [..tx ids..] } }` |

## Client behavior (`checkout_web.client`)

| Setting | Value |
| --- | --- |
| Connect timeout | 0.5 s |
| Read timeout | 2 s (reserve/release/ingest), 5 s (catalog fetch) |
| Retries | Idempotent calls only: reserve, release, ingest, catalog GET. Total reserve budget 4 s |
| Keep-alive | One pooled client per target |
| Propagation | Forward `X-Request-ID`; generate one for background work |
| Failure mapping | Exhausted retries raise `UpstreamUnavailable`, mapped to the contract's `503 INTERNAL_ERROR` |
