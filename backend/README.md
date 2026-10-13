# Self-Checkout Backend — Domain Services over a Shared Database

Week 5 of the architecture series. The frozen contract in [`spec.yaml`](./spec.yaml) is now
served by **four independently deployable domain services**, organized by business
function. They all use **one centralized PostgreSQL database** and never call each other.
An nginx **gateway** on `:8080` routes each request by path, so the unmodified Java load
client in `../load-client/` still sees one base URL.

## Services defined

| Service | Business function | Endpoints it owns | Background work it owns |
| --- | --- | --- | --- |
| **Catalog Service** | What can be sold, and at what price | `GET /items` | — |
| **Checkout Service** | Running a basket from start to payment | `POST /transactions`, `POST /transactions/{id}/items`, `POST /transactions/{id}/complete`, `GET /transactions/{id}` | Abandoned-transaction sweep; low-stock alert emission, inside the completion transaction |
| **Inventory Service** | Reporting stock that needs attention | `GET /inventory/low-stock` | — |
| **Analytics Service** | Understanding buying behaviour | `GET /analytics/popular-items` | The persistent popular-items streaming pipeline |

Infrastructure, not domain services:
- **gateway** (nginx; routing only)
- **postgres** (the one shared database)
- **seed** and **tools** (one-shot operator CLIs)

Each domain service is its own deployable unit:
- its own Python package (`services/<name>/`);
- its own pinned dependency file (`requirements.lock`);
- its own image (`services/<name>/Dockerfile`);
- its own compose entry.

It can be built, started, stopped or restarted alone, and when it's down only its own
routes fail. Those failures come back as a `503` in the contract's error shape.

## How the services share the database (and nothing else)

The services' only coupling is the database, and the code for it is packaged deliberately:

- **`checkout-data` 0.5.0** ([`packages/checkout_data/`](./packages/checkout_data/)) is the shared data-access library. It holds:
  - the schema (ORM models);
  - connection and session handling, and the advisory-lock helpers;
  - the seed and invariant CLIs;
  - **one repository module per consuming service**: `repos/catalog.py` (read-only, shared), `repos/checkout.py`, `repos/inventory.py` and `repos/analytics.py`. Every SQL statement in the system lives in one of these.
- **`checkout_data.ownership.TABLES`** declares, for every table, its single writing owner and its readers:

  | Table | Owner | Readers |
  | --- | --- | --- |
  | `catalog_item` | catalog (seed only) | all four |
  | `inventory_stock` | checkout | checkout, inventory |
  | `transaction` | checkout | checkout |
  | `transaction_item` | checkout | checkout, analytics |
  | `low_stock_alert` | checkout | inventory |
  | `popular_window_snapshot` | analytics | analytics |
- **`checkout-web` 0.5.0** ([`packages/checkout_web/`](./packages/checkout_web/)) carries the contract's error shape, request parsing, the app factory and `/healthz`. It has no database access.
- **Each image installs both libraries as regular packages**, so the shared code is distributed into every deployable unit. A schema change means bumping `checkout-data` and redeploying the services in that table's Readers column. That coupling is accepted and visible, not hidden.

Where one business function needs another's data, it reads the shared database:
- Checkout prices scans from `catalog_item`; it doesn't call Catalog.
- Inventory reports from the stock and alert rows that Checkout writes.
- Analytics tails the scan record Checkout writes. Nothing tells it a scan happened.

[`scripts/check_boundaries.py`](./scripts/check_boundaries.py) enforces all of this as part of
the test suite. It fails on:
- a service importing another service's code;
- any network client in a service;
- SQL outside `checkout_data`;
- a repository writing a table it doesn't own, or reading one it isn't a declared reader of;
- a compose environment naming another service's host.

Contract: [`specs/005-service-architecture/contracts/service-boundaries.md`](./specs/005-service-architecture/contracts/service-boundaries.md).

## Analytics: one persistent streaming pipeline

```text
TailIngest ─q1─▶ WindowFilter ─q2─▶ AggregationFilter ─q3─▶ RankingFilter ─slot─▶ OutputFilter
 (DB tail)       (slide by id)      (running counts)        (top-200)              (latest-wins, commit)
```

The Analytics Service runs one pipeline for its whole life. There is no new run per 500
scans.
- **TailIngest** follows `transaction_item` as rows commit. It keeps a contiguous watermark, so scans that commit out of order are never skipped, and an id that never commits is skipped after 1 s.
- **WindowFilter** slides the 1 000-scan window by scan id every 500.
- **AggregationFilter** keeps a running per-SKU count.
- **RankingFilter** breaks ties by SKU.
- **OutputFilter** commits each window. If several are ready at once, it writes only the newest.
- **A supervisor** holds a PostgreSQL advisory lock, so only one pipeline runs across replicas. It rebuilds the window from the database on start, so a restarted Analytics catches up on scans made while it was down.

Design: [research R5–R6](./specs/005-service-architecture/research.md) and
[data-model §3](./specs/005-service-architecture/data-model.md).

## Prerequisites

- **Docker Desktop**, running, with ≥ 8 CPUs and ≥ 8 GB memory (Settings → Resources), and Compose v2 (`docker compose version`).
- **JDK 21+** for the load client.
- **`uv`** and a native PostgreSQL 17 (`brew install postgresql@17`), only for the host-side test suite.

## Run

```bash
docker compose up -d --build --wait     # build, seed an empty DB, start everything; blocks until healthy
curl -s localhost:8080/items | head -c 120
docker compose ps                       # postgres, catalog, checkout, inventory, analytics, gateway: healthy
docker compose down -v                  # tear down, including database data
```

One service at a time:

```bash
docker compose stop analytics           # only /analytics/popular-items is affected (JSON 503)
docker compose start analytics          # it rebuilds its window from the database
docker compose up -d --build --no-deps analytics   # rebuild and redeploy just this one
```

`seed` runs `python -m checkout_data.seed --if-empty`, so it seeds only an empty database. A
fresh run therefore always starts with `docker compose down -v`.

## Test

```bash
uv sync
uv run pytest -q                        # all four service apps in-process behind the gateway's route table
uv run python scripts/check_boundaries.py
./scripts/check_independence.sh         # needs the stack up: stop/restart each service, check the rest keep working
```

The same contract tests over real HTTP, through the gateway and the containers:

```bash
docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait
GATEWAY_URL=http://localhost:8080 \
TEST_DATABASE_URL=postgresql+asyncpg://checkout:checkout@127.0.0.1:5433/checkout_test \
  uv run pytest -q tests/contract
docker compose -f compose.yaml -f compose.test.yaml down -v
```

The in-process suite runs against its own `checkout_test` database on the native
PostgreSQL. Its name must end in `_test` or the suite refuses to start.
`tests/unit/test_gateway_routes.py` checks that the test dispatcher's route table and
`gateway/nginx.conf` are identical.

## Measure

```bash
./scripts/run_load_tests.sh             # default run, then stress; fresh stack for each
./scripts/run_load_tests.sh stress      # or: default
docker compose run --rm tools python -m checkout_data.verify_invariant   # the graded correctness check
```

Each run does a fresh `down -v` and `up --wait`, then runs the load client with exactly
the assignment's flags (`./run.sh`, and `./run.sh --stations=100 --duration=120`). It moves
the report into `../load-client/reports/005-service-architecture/`, saves the compose logs to
`logs/`, verifies the invariant, and leaves the last stack up for inspection.

### Graded reports (2026-10-13)

| Report | Config | tx/s | items/s | COMPLETE err % | Invariant |
| --- | --- | --- | --- | --- | --- |
| [`report-20261013-093301.json`](../load-client/reports/005-service-architecture/report-20261013-093301.json) | default (10 stations / 60 s) | 220.6 | 4 250 | 45.4 % | PASS |
| [`report-20261013-093519.json`](../load-client/reports/005-service-architecture/report-20261013-093519.json) | stress (100 stations / 120 s) | 190.9 | 5 242 | 61.7 % | PASS |

Every error is a contract-defined `409 INSUFFICIENT_STOCK` once hot SKUs sell out.

Throughput is about 70 % of Week 4's native single-process run. Each request now pays a
Docker Desktop port-forwarding hop and a gateway hop, and the services reach the database
over TCP inside the VM. These results are therefore not directly comparable to Weeks 1–4.
The measured breakdown is in
[`../load-client/reports/README.md`](../load-client/reports/README.md#week-5--domain-services-under-docker-compose-2026-10-13).

## Inspect stock

Connect to the compose database from the host (port 5433), or from inside the container:

```bash
psql -h 127.0.0.1 -p 5433 -U checkout checkout          # password: checkout
docker compose exec postgres psql -U checkout -d checkout
```

One row for the whole catalog. `units_sold` should equal the `Units decremented` that the
invariant check reports:

```sql
SELECT count(*)                                   AS skus,
       sum(initial_stock - current_stock)         AS units_sold,
       count(*) FILTER (WHERE current_stock = 0)  AS sold_out,
       count(*) FILTER (WHERE current_stock < 50) AS below_threshold
  FROM inventory_stock;
```

What is hot right now against what is left to sell: the popular-items snapshot joined to
live stock. A high-rank SKU at `0` is what turns later baskets into `409 INSUFFICIENT_STOCK`:

```sql
SELECT r.ord AS rank, r.item->>'sku' AS sku, (r.item->>'scanCount')::int AS scans, s.current_stock
  FROM popular_window_snapshot p,
       LATERAL jsonb_array_elements(p.ranking) WITH ORDINALITY AS r(item, ord)
  JOIN inventory_stock s ON s.sku = r.item->>'sku'
 ORDER BY r.ord
 LIMIT 20;
```

How the pipeline is doing:

```bash
docker compose logs analytics | grep -E 'pipeline|window published|gap skipped' | tail
```

## How it works

| Concern | Approach | Where |
| --- | --- | --- |
| Routing | nginx, by path: `= /items` → catalog, `/transactions` → checkout, `/inventory/` → inventory, `/analytics/` → analytics, everything else → checkout (same 404 body as before) | `gateway/nginx.conf` |
| Catalog | Each service that needs it loads its own copy at startup; `GET /items` is a pre-rendered body in ascending SKU order | `checkout_data/catalog.py`, `catalog_service` |
| Scan | One CTE: bump the transaction's counters, insert the line, `RETURNING` both | `checkout_service/service.py` → `repos/checkout.py` |
| Completion | One DB transaction: lock the tx row, lock stock rows **in SKU order**, one conditional batched `UPDATE ... WHERE current_stock >= qty`, alert rows on threshold crossing. A short count rolls back as `409` | `checkout_service/service.py`, `alerts.py` |
| Never oversell | The conditional `UPDATE`; `CHECK (current_stock >= 0)` as a backstop. Correct across Checkout replicas because the locks are in the database | `repos/checkout.py` |
| Low-stock report | Live stock joined to the latest alert per SKU | `inventory_service` → `repos/inventory.py` |
| Popular items | Persistent pipeline tailing `transaction_item`; one snapshot row every worker reads | `analytics_service/pipeline.py`, `supervisor.py`, `popular_items.py` |
| Money | Integer cents end to end | `checkout_data/money.py` |

## Layout

```
compose.yaml            postgres, seed, catalog, checkout, inventory, analytics, gateway, tools
compose.test.yaml       test-stack override (checkout_test, small catalog, separate images)
gateway/                nginx.conf + Dockerfile — routing only
packages/
  checkout_data/        SHARED DATA ACCESS: schema, engine, ownership registry, repos/<service>.py,
                        catalog cache, seed and verify CLIs (+ the checkout-tools Dockerfile)
  checkout_web/         SHARED HTTP CONVENTIONS: errors, request helpers, app factory, /healthz
services/
  catalog/              catalog_service    + requirements.lock + Dockerfile
  checkout/             checkout_service   (service, alerts, sweeper)
  inventory/            inventory_service  (low_stock)
  analytics/            analytics_service  (pipeline, supervisor, popular_items)
scripts/
  run_load_tests.sh     fresh stack → load → verify, for the default and stress runs
  check_boundaries.py   the seven service-boundary rules
  check_independence.sh stop/restart each service; the rest must keep working
  lock_services.sh      regenerate the per-image requirements.lock files
  seed.py, verify_invariant.py   host shims for the checkout_data CLIs
tests/                  contract / integration / unit; _dispatch.py mirrors the gateway
```

## Design artifacts

- Week 5 (this architecture): [`specs/005-service-architecture/`](./specs/005-service-architecture/):
  - [spec](./specs/005-service-architecture/spec.md)
  - [plan](./specs/005-service-architecture/plan.md)
  - [research](./specs/005-service-architecture/research.md)
  - [data model](./specs/005-service-architecture/data-model.md)
  - [contracts](./specs/005-service-architecture/contracts/)
  - [quickstart](./specs/005-service-architecture/quickstart.md)
  - [tasks](./specs/005-service-architecture/tasks.md)
- Week 4 (streaming pipeline): [`specs/004-streaming-analytics/`](./specs/004-streaming-analytics/)
- Week 3 (pipelined analytics): [`specs/003-pipelined-analytics/`](./specs/003-pipelined-analytics/)
- Week 2 (layering): [`specs/002-layered-architecture/`](./specs/002-layered-architecture/)
- Week 1 (behaviour, schema, contract; still authoritative): [`specs/001-checkout-backend/`](./specs/001-checkout-backend/)
