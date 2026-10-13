# Internal Contract: Service Boundaries

**Feature**: `005-service-architecture`

This replaces 002's `layer-boundaries.md` as the internal contract. The layering rules
inside each service carry over: router → business logic → repository; no SQL outside
`checkout_data`; the repository layer never commits.

## Packages and allowed dependencies

```text
catalog_service    ──▶ checkout_web, checkout_data, checkout_data.repos.catalog
checkout_service   ──▶ checkout_web, checkout_data, checkout_data.repos.checkout, checkout_data.repos.catalog
inventory_service  ──▶ checkout_web, checkout_data, checkout_data.repos.inventory
analytics_service  ──▶ checkout_web, checkout_data, checkout_data.repos.analytics, checkout_data.repos.catalog

checkout_web       ──▶ fastapi, orjson                  (never checkout_data.repos, never SQL)
checkout_data      ──▶ sqlalchemy, asyncpg, psycopg2    (never fastapi, never checkout_web)
```

## Forbidden (each is a rule in `scripts/check_boundaries.py`, research R12)

1. Any `<a>_service` module importing any `<b>_service` module (a ≠ b).
2. Any service module importing `httpx`, `requests`, `aiohttp`, `urllib.request`, `http.client` or `socket`. Services make no outbound network calls except to the database driver.
3. A service importing `checkout_data.repos.<x>` where `x` is neither its own name nor `catalog`.
4. SQL in a service: `sqlalchemy.text`, `select`, `insert`, `update` or `delete` imported anywhere under `services/`.
5. A statement in `checkout_data/repos/<svc>.py` that writes a table whose owner isn't `<svc>`, or reads a table whose readers don't include `<svc>`, per `checkout_data.ownership.TABLES` ([data-model.md §1](../data-model.md)).
6. A domain service's compose `environment` referencing another domain service's hostname (`catalog`, `checkout`, `inventory`, `analytics`) or the gateway.
7. `checkout_data` importing `fastapi` or `checkout_web`, or raising `ApiError` (002 rule, kept).

## Verifying

```bash
uv run python scripts/check_boundaries.py      # prints violations; exit 1 if any
uv run pytest -q tests/unit/test_boundaries.py # same check, as part of the suite
```

Expected output once complete: `0 violations (7 rules, 4 services, 6 tables)`.

## Deliberate coupling, stated plainly

Services are coupled through the **schema** and the **`checkout_data` version**. A change to
a table's shape bumps `checkout_data` and requires redeploying every service in that
table's Readers column. That's the price of a shared database, and it's accepted (spec Edge
Cases). Services are *not* coupled through timing, availability or addresses: any one of
them can be down while the others keep serving (spec SC-003).
