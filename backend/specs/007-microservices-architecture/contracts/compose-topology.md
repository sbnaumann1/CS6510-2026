# Contract: Compose Topology

Supersedes [005's topology](../../005-service-architecture/contracts/compose-topology.md).
Start: `docker compose up -d --build --wait`. Stop: `docker compose down -v`.

## Units

| Unit | Image | Role | Published port |
| --- | --- | --- | --- |
| `checkout-db`, `inventory-db` | `postgres:17` | One database each (Catalog has a file, Analytics has nothing persistent) | `127.0.0.1:5442`–`5443` (debugging only) |
| `seed-catalog`, `seed-checkout`, `seed-inventory` | the service's own image | One-shot: create schema and seed owned data using that service's role (`seed-catalog` writes `catalog.csv` to the `catalog_data` volume instead) | none |
| `catalog`, `checkout`, `inventory`, `analytics` | one image per service | Microservices | `127.0.0.1:8081`–`8084` (debugging and gateway-bypass checks) |
| `gateway` | nginx | Public entry point | `8080` |
| `tools` (profile `tools`) | `checkout-tools` | `verify_invariant`, calls internal reports over `mesh` | none |

## Networks

| Network | Members | Purpose |
| --- | --- | --- |
| `edge` | gateway, catalog, checkout, inventory, analytics | Public traffic |
| `mesh` | catalog, checkout, inventory, analytics, tools | Service-to-service calls |
| `checkout_data` | checkout-db, seed-checkout, checkout | Checkout's private store |
| `inventory_data` | inventory-db, seed-inventory, inventory | Inventory's private store |

A database network contains **exactly one service**. Host ports on the databases are bound to
`127.0.0.1` only and are not on any service network.

## Volumes

| Volume | Mounted into | Mode |
| --- | --- | --- |
| `catalog_data` | `seed-catalog` | read-write |
| `catalog_data` | `catalog` | **read-only** |

No other unit mounts it. `catalog.csv` is the only file on it.

## Credentials

Each of the two databases has its own role and password (`<svc>_user` / `<svc>_pass` in `.env.example`). Catalog has no credentials: its protection is the read-only volume mount.
A service receives only its own `DATABASE_URL`. No service's environment contains another
database's host or credentials.

## Environment per service

| Service | Needs |
| --- | --- |
| catalog | `CATALOG_FILE=/data/catalog.csv`, `CATALOG_SIZE` (seed job only), `WORKERS=2` (no `DATABASE_URL`) |
| checkout | `DATABASE_URL`, `INVENTORY_URL=http://inventory:8000`, `CATALOG_URL=http://catalog:8000`, `TX_ABANDON_MINUTES`, `RECOVERY_LEASE_S=10`, `WORKERS=6`, `DB_POOL_SIZE=10` |
| inventory | `DATABASE_URL`, `CATALOG_URL`, `LOW_STOCK_THRESHOLD`, `STOCK_PER_ITEM`, `CATALOG_SIZE`, `WORKERS=2`, `DB_POOL_SIZE=8` |
| analytics | `CATALOG_URL=http://catalog:8000`, `CHECKOUT_URL=http://checkout:8000`, `POPULAR_*` (unchanged from 005), `WORKERS=1` (no `DATABASE_URL`, no volume) |

Peer service URLs are allowed; peer **database** coordinates are not. Analytics runs exactly one
instance (`WORKERS=1`, one container, no replicas): it needs no leader election because it writes
nothing shared, but two instances would only duplicate work ([research R5](../research.md)).

## Startup order

1. each `*-db` healthy
2. each `seed-*` completed successfully (`seed-catalog`, `seed-checkout`, `seed-inventory`, in parallel; `seed-catalog` needs no database; Analytics has no seed)
3. each service started and healthy. Services do **not** wait for each other: peers are
   contacted lazily, so any one service can start while another is down
4. gateway started (waits for all four healthy, as in 005)

## Database tuning

`synchronous_commit=off`, `wal_compression=on`, `checkpoint_timeout=15min`, `max_wal_size=2GB`.
`shared_buffers`: checkout 512 MB, inventory 128 MB.
`max_connections`: checkout 100, inventory 60. Each must exceed
`workers × pool_size` of its service plus seed and report slack.

## Independence checks

`scripts/check_independence.sh` (updated): builds one service's image alone, restarts it with the
others running, and confirms the others stay healthy without a rebuild.
