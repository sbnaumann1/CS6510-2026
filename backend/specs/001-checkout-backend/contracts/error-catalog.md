# Error Catalog

Every error response body is the contract's `ApiError` shape — exactly two string fields:

```json
{ "error": "TRANSACTION_NOT_OPEN", "message": "Transaction tx-4821 is already completed." }
```

`error` is a stable machine-readable code; `message` is human-readable and may include the
transaction ID or SKU. No other top-level fields, ever.

## Codes

| Code | HTTP | Triggered when |
| --- | --- | --- |
| `INVALID_REQUEST` | 400 | Body is not valid JSON, `stationId` missing/empty/not a string on `POST /transactions`, `sku` missing/not a string on a scan, or a query parameter (`threshold`, `limit`) is not a positive integer |
| `TRANSACTION_NOT_FOUND` | 404 | No transaction with that ID, or the ID does not match `tx-<digits>` |
| `SKU_NOT_FOUND` | 404 | Scanned SKU is not in the catalog |
| `TRANSACTION_NOT_OPEN` | 409 | Scan or complete against a `COMPLETED` or `CANCELLED` transaction |
| `EMPTY_BASKET` | 409 | Complete called on an OPEN transaction with zero scanned units |
| `INSUFFICIENT_STOCK` | 409 | At least one SKU in the basket has `current_stock < quantity`; nothing is decremented |
| `INTERNAL_ERROR` | 500 | Unhandled exception — catch-all handler; none expected under normal load |

## Per endpoint

| Endpoint | Success | Possible errors |
| --- | --- | --- |
| `GET /items` | 200 | — (catalog is preloaded; a failure here is `INTERNAL_ERROR`) |
| `POST /transactions` | **201** | `INVALID_REQUEST` |
| `POST /transactions/{id}/items` | 200 | `INVALID_REQUEST`, `TRANSACTION_NOT_FOUND`, `SKU_NOT_FOUND`, `TRANSACTION_NOT_OPEN` |
| `POST /transactions/{id}/complete` | 200 | `TRANSACTION_NOT_FOUND`, `TRANSACTION_NOT_OPEN`, `EMPTY_BASKET`, `INSUFFICIENT_STOCK` |
| `GET /transactions/{id}` | 200 | `TRANSACTION_NOT_FOUND` |
| `GET /inventory/low-stock` | 200 | `INVALID_REQUEST` (bad `threshold`) |
| `GET /analytics/popular-items` | 200 | `INVALID_REQUEST` (bad `limit`) |

## Precedence

When more than one condition holds, respond with the first match:

1. `INVALID_REQUEST` — malformed input, before any lookup
2. `TRANSACTION_NOT_FOUND` — before inspecting status
3. `SKU_NOT_FOUND` — resolved from the in-memory catalog, before touching the transaction
4. `TRANSACTION_NOT_OPEN`
5. `EMPTY_BASKET`
6. `INSUFFICIENT_STOCK` — last, since it requires locking stock rows

## FastAPI defaults that must be overridden

These are the two places the framework silently breaks the contract:

| Default behaviour | Required behaviour | Mechanism |
| --- | --- | --- |
| Validation failure → `422` with `{"detail": [...]}` | `400` with `{"error":"INVALID_REQUEST","message":...}` | `@app.exception_handler(RequestValidationError)` |
| `HTTPException` → `{"detail": "..."}` | `{"error": ..., "message": ...}` | `@app.exception_handler(HTTPException)` + a custom exception carrying the code |
| `POST` route returns `200` | `POST /transactions` returns `201` | `status_code=201` on the route decorator |

A 500 from an unhandled exception still returns the `ApiError` shape; the traceback goes to the
server log, never into the response body.
