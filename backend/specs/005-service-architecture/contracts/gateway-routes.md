# Internal Contract: Gateway Route Table

**Feature**: `005-service-architecture`

The external contract ([`spec.yaml`](../../../spec.yaml), 001 `contracts/openapi.yaml` and
`error-catalog.md`) is frozen. The gateway is the single public address
(`http://localhost:8080`) and does **routing only**: no validation, no rewriting, no
database access, no caching (spec FR-004).

## Route table

`gateway/nginx.conf` must implement exactly this table. The in-process test dispatcher in
`tests/conftest.py` must implement the same table. `tests/unit/test_gateway_routes.py` parses
both and fails if they differ.

| nginx `location` | Matches (contract operations) | Upstream |
| --- | --- | --- |
| `= /items` | `GET /items` | `catalog:8000` |
| `/transactions` (prefix) | `POST /transactions`, `POST /transactions/{id}/items`, `POST /transactions/{id}/complete`, `GET /transactions/{id}` | `checkout:8000` |
| `/inventory/` (prefix) | `GET /inventory/low-stock` | `inventory:8000` |
| `/analytics/` (prefix) | `GET /analytics/popular-items` | `analytics:8000` |
| `= /gateway/healthz` | gateway liveness (not in the contract) | answered by nginx: `200 {"status":"ok","service":"gateway"}` |
| `/` (default) | any other path | `checkout:8000`, so unknown paths get the same FastAPI 404 `ApiError` body as before |

Services' own `GET /healthz` is not given a route. A request for `:8080/healthz` falls
through the default location to Checkout's health endpoint. That is harmless (no data, not in
the contract), and it's left that way rather than adding a gateway-only special case.

`/transactions/{id}/items` must go to **checkout**, not catalog. The exact-match `= /items`
guarantees that, and the route-parity test checks it.

## Proxy behavior

- The method, path, query string and body are forwarded unchanged. Upstream responses, error bodies included, are passed through unchanged.
- Upstream connections use HTTP/1.1 keepalive (`proxy_http_version 1.1`, `Connection ""`, `keepalive 64`). The client's `Upgrade`/`HTTP2-Settings` headers are not forwarded.
- Upstreams are declared with `resolve`, so a restarted container's new IP is picked up within 5 s.

## Behavior when an upstream is down

| Condition | Status | Body |
| --- | --- | --- |
| Upstream refuses or the connection resets (502), no upstream is available (503), or it times out (504) | `503` | `{"error":"INTERNAL_ERROR","message":"<service> service is unavailable."}` |

The body keeps the contract's two-field `ApiError` shape. Only routes whose upstream is down
are affected (spec SC-003). Proxy timeouts: connect 2 s, read 15 s. The load client's
per-request timeout is 10 s, so the gateway never times out before the client does.
