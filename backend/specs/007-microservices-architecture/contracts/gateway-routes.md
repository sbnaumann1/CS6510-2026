# Contract: Gateway Routes

nginx, port `8080`, the only published application port. Same routes as
[005's table](../../005-service-architecture/contracts/gateway-routes.md), plus two additions.

| Match | Target | On 502/503/504 |
| --- | --- | --- |
| exact `/items` | catalog | `503` contract-shaped body naming catalog |
| prefix `/transactions` | checkout | `503` naming checkout |
| prefix `/inventory/` | inventory | `503` naming inventory |
| prefix `/analytics/` | analytics | `503` naming analytics |
| prefix `/internal/` | **none**: `404 {"error":"NOT_FOUND","message":"Not found."}` | n/a |
| exact `/gateway/healthz` | gateway itself | n/a |
| default `/` | checkout (unknown paths keep the 005 `404` body) | `503` naming checkout |

Additions over 005:

1. **`/internal/` is blocked.** Without this rule the default route would forward
   `/internal/...` to Checkout. The in-process twin in `tests/_dispatch.py` implements the same
   rule, and `tests/unit/test_gateway_routes.py` asserts the two tables match.
2. **`X-Request-ID`**: the gateway sets `X-Request-ID $request_id` (keeping an incoming one if
   present) on every proxied request, so the id exists from the first hop.

Everything else (keepalive upstreams, cleared `Upgrade`/`HTTP2-Settings`, 2 s connect timeout,
15 s read timeout, `proxy_intercept_errors off`) is unchanged. No location may set its own
`proxy_set_header`.
