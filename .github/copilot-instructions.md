# Copilot Instructions

## Project shape

This is the Week 7 self-checkout implementation: four Python 3.11+ microservices behind an Nginx gateway, with the frozen public contract in `backend/spec.yaml`. The Java load client in `load-client/` remains the external consumer and calls the gateway on port 8080.

- **Catalog** owns the read-only `catalog.csv` volume and serves `GET /items`.
- **Checkout** owns `checkout-db`, transactions, scanned items, and the completion saga.
- **Inventory** owns `inventory-db`, stock, reservations, and low-stock alerts.
- **Analytics** has no database. Its single, in-memory pipeline polls Checkout's `/internal/scans` log and rebuilds after a restart.
- `backend/packages/checkout_web` is the only shared package. It provides app setup, error shapes, request IDs, money formatting, and the service HTTP client; it must stay schema- and database-free.

Compose enforces private stores with separate database networks. Public traffic goes through `backend/gateway/nginx.conf`; `/internal/*` is service-to-service only and must never be exposed by the gateway. The test-only equivalents are `tests/_dispatch.py` (gateway routes) and `MeshTransport` (service calls plus fault injection), so update their route expectations when changing public routing.

## Commands

Run backend commands from `backend/`.

```bash
uv sync
uv run pytest -q
uv run pytest -q tests/failure/test_saga_idempotency.py::test_ten_retries_leave_the_state_of_completing_once
uv run python scripts/check_boundaries.py
```

The in-process suite uses native PostgreSQL 17 and creates only `checkout_checkout_test` and `checkout_inventory_test`; it refuses non-`_test` database names. There is no separate formatter or linter configuration. `check_boundaries.py` is the architecture validation and is also covered by the tests.

```bash
docker compose up -d --build --wait
docker compose down -v
docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait
GATEWAY_URL=http://localhost:8080 uv run pytest -q tests/contract
docker compose -f compose.yaml -f compose.test.yaml down -v
```

The real-stack tests use disposable Compose volumes; start each gateway-mode run with `down -v`. For an end-to-end load run, build and run `load-client/` with `./build.sh` and `./run.sh`.

## Service-boundary conventions

- Keep all service code within its own `*_service` package. A service may import `checkout_web`, but never another service package or another service's models/repositories.
- Only Checkout and Inventory use database libraries, and each may name only tables in its own `models.py`. Catalog and Analytics have no database credentials or database dependencies.
- Use `checkout_web.client` for every outbound service call. Allowed calls are Checkout → Catalog/Inventory, Inventory → Catalog, and Analytics → Catalog/Checkout. Do not introduce raw `httpx`, `requests`, sockets, or a shared data layer in service code.
- Create service apps through `checkout_web.create_service_app` so ORJSON responses, API error mapping, `/healthz`, and `X-Request-ID` propagation stay consistent.

## Correctness-critical flows

`POST /transactions/{id}/complete` is a saga, not a distributed database transaction. Checkout records `OPEN → COMPLETING`, calls Inventory to reserve stock, then finalizes locally; on an uncertain or failed outcome it compensates with release and leaves recovery to finish any undelivered rollback. Preserve the completion epoch and Inventory's `(transaction_id, epoch)` idempotency record so duplicate requests, lost responses, retries, and recovery cannot apply stock changes twice.

Inventory reservations normalize baskets and lock stock rows in SKU order inside one local transaction. Do not hold database locks across an HTTP call or weaken the atomic check/decrement/alert/adjustment operation: stock must never go negative, and a short basket must not partially complete.

Analytics must remain pull-based and decoupled from Checkout: Checkout does not notify Analytics. Its persistent pipeline maintains a contiguous scan-id watermark, handles gaps, publishes the latest completed window in memory, and runs exactly one instance. If Checkout is unavailable, Analytics serves its last ranking (or an empty window after restart) and catches up later.

## Tests and configuration

`tests/conftest.py` sets per-service database URLs and the temporary catalog path before importing service modules because service settings and engines bind at import time. The default `client` waits for Analytics to consume Checkout scans before `/analytics/*` reads; use `checkout_client` or direct pipeline fixtures when testing asynchronous pipeline timing itself.

Keep the public OpenAPI behavior stable. Contract tests are shared across architecture iterations; place architecture-specific fault, concurrency, recovery, and boundary coverage in the existing `failure/`, `integration/`, or `unit/` suites as appropriate.
