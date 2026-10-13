# Internal Contract: Compose Topology

**Feature**: `005-service-architecture` | File: `backend/compose.yaml` (+ `compose.test.yaml` override)

## Units

| Compose service | Image (build) | Role | Ports (host → container) | Depends on | Health check |
| --- | --- | --- | --- | --- | --- |
| `postgres` | `postgres:17` | the one centralized database | `127.0.0.1:5433 → 5432` | — | `pg_isready -U checkout -d $POSTGRES_DB` |
| `seed` | `packages/checkout_data/Dockerfile` (`checkout-tools`) | one-shot: `python -m checkout_data.seed --if-empty` (seeds only an empty database) | — | `postgres` healthy | exits 0 |
| `catalog` | `services/catalog/Dockerfile` | Catalog Service | `127.0.0.1:8081 → 8000` | `seed` completed | `GET /healthz` |
| `checkout` | `services/checkout/Dockerfile` | Checkout Service | `127.0.0.1:8082 → 8000` | `seed` completed | `GET /healthz` |
| `inventory` | `services/inventory/Dockerfile` | Inventory Service | `127.0.0.1:8083 → 8000` | `seed` completed | `GET /healthz` |
| `analytics` | `services/analytics/Dockerfile` | Analytics Service | `127.0.0.1:8084 → 8000` | `seed` completed | `GET /healthz` |
| `gateway` | `gateway/Dockerfile` (`nginx:1.27-alpine`) | path router | `8080 → 8080` | all four services healthy | `GET /gateway/healthz` |
| `tools` | `checkout-tools` (profile `tools`) | on-demand CLIs (verify, seed) | — | `postgres` healthy | — |

The direct service ports (8081–8084) are bound to loopback and exist only for the US3
bypass test and debugging. The load client always uses 8080.

Domain services depend only on `seed`, never on each other. That is why
`docker compose up -d analytics` starts the database and the seed step, but no other
domain service (spec FR-002). The seed step can re-run safely at that point because
`--if-empty` is a no-op on a populated database: re-running it never wipes data under live
services. A full reset happens only through `docker compose down -v` (or an explicit
`docker compose run --rm tools python -m checkout_data.seed --reset` with every domain
service stopped).

## Commands (spec FR-005)

| Intent | Command (from `backend/`) |
| --- | --- |
| Build and start everything, block until healthy | `docker compose up -d --build --wait` |
| Tear down, including DB data | `docker compose down -v` |
| Stop / start / restart one unit | `docker compose stop analytics` · `docker compose start analytics` · `docker compose restart analytics` |
| Rebuild and redeploy one unit | `docker compose up -d --build --no-deps analytics` |
| Verify the invariant | `docker compose run --rm tools python -m checkout_data.verify_invariant` |
| Test stack (`checkout_test`, small catalog) | `docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait` |

## Environment

As in [data-model.md §4](../data-model.md). Only `DATABASE_URL` and tuning values are set,
and no service is given another service's address.

## Resource prerequisite

Docker Desktop must be set to ≥ 8 CPUs and ≥ 8 GB memory before measured runs (research R9).
