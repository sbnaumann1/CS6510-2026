# Contracts

## The contract is frozen

`openapi.yaml` here is a byte-identical copy of `backend/spec.yaml`, which is itself identical to
the course-provided `spec/self-checkout-openapi.yaml` (verified 2026-09-16). **Do not edit any of
them.** The Java load client in `load-client/` is compiled against this contract and is never
modified between architecture weeks — that is what makes week-to-week comparison meaningful. Any
mismatch shows up as a client-side `ApiException`, counted as an error in the graded report.

This implementation conforms to the contract; the contract is never adjusted to fit the
implementation.

## What the client actually asserts

Read from `load-client/src/ApiClient.java` — these are the checks that fail a run:

| Call | Asserted status | Fields read from the body (missing ⇒ failure) |
| --- | --- | --- |
| `GET /items` | 200 | `items[].sku`, `items[].name`, `items[].price` |
| `POST /transactions` | **201** | `transactionId`, `stationId`, `status` |
| `POST /transactions/{id}/items` | 200 | `sku`, `unitPrice`, `itemCount`, `runningTotal` |
| `POST /transactions/{id}/complete` | 200 | `itemCount`, `totalAmount` |
| `GET /inventory/low-stock` | 200 | `alerts[].sku`, `.name`, `.currentStock`, `.threshold` |
| `GET /analytics/popular-items?limit=N` | 200 | `items[].sku`, `.name`, `.scanCount`, `.rank` |

Any other status code — including a `409` that is correct per the contract — is recorded as an
error by the client. See research R13 for why legitimate `409 INSUFFICIENT_STOCK`
responses are expected during a default run.

Additional client-visible details worth pinning down in contract tests:

- The client sends `POST /transactions/{id}/complete` with a body of `{}` and
  `Content-Type: application/json` — the endpoint must accept a body and must not require one.
- Transaction IDs are URL-encoded by the client, so the server must accept the encoded form of
  whatever it issued. Keeping IDs to `tx-<digits>` (research R5) sidesteps this entirely.
- `java.net.http.HttpClient` negotiates HTTP/2 and falls back to HTTP/1.1 with keep-alive; uvicorn
  serves HTTP/1.1. Connection reuse must work — do not disable keep-alive.
- Catalog **response order defines popularity rank** in `ItemSampler.java`. Return items in
  ascending SKU order, always.

## Conformance test strategy

`tests/contract/` holds one module per endpoint, asserting status code, exact field names, and
JSON types against the schemas in `openapi.yaml` — including every error path in
[error-catalog.md](./error-catalog.md). These tests are the regression net for FR-012 and must
pass before any load run is treated as valid.

FastAPI's own generated schema at `/openapi.json` is *not* the contract and will differ in
incidental ways (titles, descriptions); do not diff against it.

## Files

| File | Purpose |
| --- | --- |
| `openapi.yaml` | Frozen copy of the shared contract (read-only reference) |
| `error-catalog.md` | Error code ↔ HTTP status ↔ trigger, per endpoint |
