# Contract: Compose Topology

Starting point: [007's topology](../../007-microservices-architecture/contracts/compose-topology.md).
This page lists what changes. Command remains
`docker compose up -d --build --wait` / `docker compose down -v`.

## Units added or removed

| Unit | Change |
| --- | --- |
| `kafka` | **new**: `apache/kafka`, KRaft combined mode, one broker, `KAFKA_HEAP_OPTS=-Xmx512m`, volume `kafka_data`, health check = list topics with the bundled CLI |
| `kafka-init` | **new**, one-shot: creates every topic in `events/topics.yaml` idempotently (`--if-not-exists`), then exits `0` |
| `catalog`, `checkout`, `inventory`, `analytics` | no peer URLs in the environment (`CATALOG_URL`, `INVENTORY_URL`, `CHECKOUT_URL` removed); `KAFKA_BOOTSTRAP=kafka:9092` added |
| `gateway` | unchanged; joins each `edge_<svc>` network |
| `tools` (operator check) | joins each `edge_<svc>` network to read `/internal/reports/*`; not a microservice |
| `mesh` network | **removed** |

## Networks

| Network | Members | Purpose |
| --- | --- | --- |
| `edge_catalog`, `edge_checkout`, `edge_inventory`, `edge_analytics` | gateway + that one service (+ tools) | public routes |
| `bus_catalog`, `bus_checkout`, `bus_inventory`, `bus_analytics` | Kafka + that one service | the only path between services |
| `checkout_data`, `inventory_data` | one database + its one service (+ its seed) | private stores, as in 007 |

**No two services share a network.** A service that tried to reach a peer would fail name
resolution. Kafka is on every `bus_*` network and advertises the single listener `kafka:9092`.

## Startup order

```text
checkout-db, inventory-db, kafka        (stores and broker)
   → seed-catalog, seed-checkout, seed-inventory, kafka-init
      → catalog, checkout, inventory, analytics   (each waits only for its own seed and kafka-init)
         → gateway
```

Services never wait for each other. Each starts its Kafka producer/consumer lazily with retry, so
any service can start while the broker or a peer is down.

**Health**: `GET /healthz` returns ready only when the service's bus work is ready:

| Service | Ready when |
| --- | --- |
| Catalog | its store is readable (publishing runs in the background and retries) |
| Checkout | database reachable, reply consumer assigned and positioned, catalog snapshot ready |
| Inventory | database reachable, `inventory` group consumer assigned, catalog snapshot ready |
| Analytics | `analytics` consumer assigned, window rebuilt to the topic's end, catalog snapshot ready |

`--wait` therefore blocks until the whole event path works, so the load client never starts against
a cold snapshot.

## Credentials and access

Kafka is unauthenticated on the private bus networks (one machine, no external listener); the host
port `127.0.0.1:9094` is published for operator tooling only. Databases keep per-service roles on
private networks, exactly as in 007.

## Failure drills (scripts)

| Script | Does | Expectation |
| --- | --- | --- |
| `check_isolation.sh` | from inside each service container, resolve/connect to every peer service | none resolves; the broker does |
| `check_no_peer_calls.sh` | run the contract flow with every network except `bus_*` and `edge_*` detached from services | all public endpoints work |
| `check_independence.sh` | stop each service in turn, call every public endpoint | only dependants fail, within 5 s, in the contract's error shape |
| `check_broker_outage.sh` | stop Kafka during load, restart it | nothing lost; the invariant holds; no transaction stuck |
| `check_crash_recovery.sh` | kill Checkout, then Inventory, under load | the invariant holds; no transaction stuck |
