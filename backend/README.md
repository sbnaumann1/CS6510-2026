# Self-Checkout Backend — Microservices Talking Through Kafka

Week 8 of the architecture series. The frozen contract in [`spec.yaml`](./spec.yaml) is served
by **four independently deployable microservices**, each with a private data store, as in
Week 7. What changed is how they cooperate: **no microservice calls another.** Every
service-to-service interaction is an **event on one Kafka broker**. The only HTTP is shopper →
gateway → the service that owns the route, which is the public contract and did not change, so
the unmodified Java load client in `../load-client/` still sees one base URL on `:8080`.

## Architecture

![Architecture diagram](./docs/architecture.svg)

The diagram above is [`docs/architecture.svg`](./docs/architecture.svg); the same diagram as
Mermaid (which GitHub renders in place), the topic table, and the completion sequence are in
[`docs/architecture.md`](./docs/architecture.md).

```mermaid
flowchart LR
    LC(["Load client / shopper"]) -->|"public HTTP contract"| GW["API Gateway (nginx)"]
    GW -->|HTTP| CAT["Catalog<br/>catalog.csv (read-only file)"]
    GW -->|HTTP| CHK["Checkout<br/>checkout-db (PostgreSQL)"]
    GW -->|HTTP| INV["Inventory<br/>inventory-db (PostgreSQL)"]
    GW -->|HTTP| ANA["Analytics<br/>in-memory window"]

    subgraph K["Kafka event broker"]
        T1[["catalog.items.v1"]]
        T2[["checkout.scans.v1"]]
        T3[["checkout.completions.v1"]]
        T4[["inventory.stock.v1"]]
        T5[["dead-letters.v1"]]
    end

    CAT ==> T1
    T1 ==> CHK
    T1 ==> INV
    T1 ==> ANA
    CHK ==> T2
    T2 ==> ANA
    CHK ==> T3
    T3 ==> INV
    INV ==> T4
    T4 ==> CHK
    CHK -.-> T5
    INV -.-> T5
    ANA -.-> T5
```

Thin arrows are HTTP, thick arrows are events. There is no arrow between two services that does
not go through the broker, and the deployment enforces it: no two services share a Docker
network, so a service cannot even resolve a peer's name.

### Full system view

Everything that runs in [`compose.yaml`](./compose.yaml): the services with their private
stores and the event machinery inside each, the one-shot jobs, and the networks that keep the
services apart. Solid arrows are HTTP, thick arrows are events, dotted arrows are one-shot
setup or failure paths.

```mermaid
flowchart TB
    LC(["Load client / shopper"])
    OPS(["Operator"])

    subgraph EDGE["edge_‹service› networks: gateway ↔ exactly one service each"]
        GW["API Gateway (nginx)<br/>routes public paths<br/>blocks /internal/*"]
    end
    LC -->|"HTTP :8080"| GW

    subgraph CATS["Catalog"]
        CAT["Catalog API<br/>GET /items<br/>publisher"]
        CATF[("catalog.csv<br/>read-only volume")]
        CAT --- CATF
    end

    subgraph CHKS["Checkout"]
        CHK["Checkout API<br/>transactions, scans,<br/>completion saga"]
        CHKX["outbox sweeper · scan relay<br/>recovery loop (lease 10 s)<br/>reply waiter (3 s)"]
        CHKDB[("checkout-db<br/>PostgreSQL 17<br/>transactions · scans ·<br/>completions · outbox")]
        CHK --- CHKX
        CHK --- CHKDB
        CHKX --- CHKDB
    end

    subgraph INVS["Inventory"]
        INV["Inventory API<br/>GET /inventory/low-stock"]
        INVX["reservation handler<br/>outbox sweeper"]
        INVDB[("inventory-db<br/>PostgreSQL 17<br/>stock · reservations ·<br/>alerts · outbox")]
        INV --- INVX
        INV --- INVDB
        INVX --- INVDB
    end

    subgraph ANAS["Analytics (single instance, no database)"]
        ANA["Analytics API<br/>GET /analytics/popular-items"]
        PIPE["KafkaScanSource → Window → Aggregation<br/>→ Ranking → Output<br/>in-memory 1 000-scan window"]
        ANA --- PIPE
    end

    GW -->|"/items"| CAT
    GW -->|"/transactions*"| CHK
    GW -->|"/inventory/*"| INV
    GW -->|"/analytics/*"| ANA
    OPS -.->|"docker compose run tools<br/>/internal/reports/*"| GW

    subgraph BUS["bus_‹service› networks: Kafka ↔ exactly one service each"]
        subgraph K["Kafka: one broker, KRaft, no ZooKeeper"]
            T1[["catalog.items.v1<br/>compacted · key SKU"]]
            T2[["checkout.scans.v1<br/>1 partition · 7 d"]]
            T3[["checkout.completions.v1<br/>6 partitions · key tx id"]]
            T4[["inventory.stock.v1<br/>6 partitions · key tx id"]]
            T5[["dead-letters.v1<br/>14 d"]]
        end
    end

    CAT ==>|"items + marker<br/>once per start"| T1
    T1 ==>|"price + name snapshot"| CHK
    T1 ==>|"name snapshot"| INV
    T1 ==>|"name snapshot"| ANA

    CHKX ==>|"scan-recorded<br/>(scan table is the outbox)"| T2
    T2 ==>|"replay last 2 × window<br/>on restart"| PIPE

    CHKX ==>|"stock-reservation-requested<br/>stock-release-requested"| T3
    T3 ==> INVX
    INVX ==>|"stock-reserved · stock-rejected<br/>stock-released · reservation-refused"| T4
    T4 ==>|"reply wakes the<br/>waiting request"| CHKX

    CHK -.->|"5 failed tries"| T5
    INV -.->|"5 failed tries"| T5
    ANA -.->|"5 failed tries"| T5

    subgraph JOBS["One-shot jobs (run once, exit 0)"]
        KI["kafka-init<br/>creates topics from<br/>events/topics.yaml"]
        SC["seed-catalog"]
        SK["seed-checkout"]
        SI["seed-inventory"]
    end
    KI -.->|creates| K
    SC -.->|writes| CATF
    SK -.->|writes| CHKDB
    SI -.->|writes| INVDB
```

How to read it:
- **Each service owns exactly one store** (a file, a PostgreSQL database, or memory) and the only way in is that service. Seed jobs write only their own service's store.
- **Checkout and Inventory pair every state change with an outbox row** in one local transaction. The event is published right after the commit, and the sweeper publishes anything left behind, so no event is lost and none is announced without its state change.
- **Request/reply across services is two events**, keyed by transaction id so both land in one partition. Checkout's HTTP request holds open until the reply event arrives or 3 s pass.
- **Isolation is topological**: each service sits on its own `edge_‹service›` network (gateway only), its own `bus_‹service›` network (Kafka only), and, for the two databases, its own private data network. No network contains two services.
- **Start-up order** is only "own seed job and `kafka-init` first". Services never wait for each other; they rebuild from the retained topics when a peer returns.

## Microservices

| Service | Business function | Endpoints it owns | Its private store |
| --- | --- | --- | --- |
| **Catalog** | What can be sold, and at what price | `GET /items` | `catalog.csv`, a **read-only file** on a volume only Catalog (and its seed job) mounts |
| **Checkout** | Running a basket from start to payment | `POST /transactions`, `POST /transactions/{id}/items`, `POST /transactions/{id}/complete`, `GET /transactions/{id}` | **`checkout-db`**: PostgreSQL 17 with transactions, scans, the record of each completion in flight, and its outbox |
| **Inventory** | How much is on hand, and what needs restocking | `GET /inventory/low-stock` | **`inventory-db`**: PostgreSQL 17 with stock, reservations, low-stock alerts, and its outbox |
| **Analytics** | Understanding buying behaviour | `GET /analytics/popular-items` | **None.** A rolling window held in memory, rebuilt from the scan topic |

Infrastructure, not services: the **gateway** (nginx, routing only), the **Kafka broker** (one
broker, KRaft, no ZooKeeper), the **seed jobs** (one per store, run once), the **topic-creation
job** and **tools** (the cross-store verification, on demand).

## How the services communicate

| Topic | Producer (only) | Consumers | Key | Carries |
| --- | --- | --- | --- | --- |
| `catalog.items.v1` (compacted) | Catalog | Checkout, Inventory, Analytics | SKU | every item once at startup, then a marker with the count |
| `checkout.scans.v1` | Checkout (scan relay) | Analytics | none (log order) | `scan-recorded`, in scan-id order |
| `checkout.completions.v1` | Checkout (outbox) | Inventory | transaction id | `stock-reservation-requested`, `stock-release-requested` |
| `inventory.stock.v1` | Inventory (outbox) | Checkout | transaction id | `stock-reserved`, `stock-rejected`, `stock-released`, `reservation-refused` |
| `dead-letters.v1` | any consumer | operators | source topic | an event that failed five times, with its error |

Every event is one JSON envelope (`eventId`, `type`, `version`, `correlationId`, `occurredAt`,
`producer`, `data`) with a JSON Schema in [`events/schemas/`](./events/schemas/); the topology is
declared in [`events/topics.yaml`](./events/topics.yaml). Shapes:
[`contracts/events.md`](./specs/008-event-driven-kafka/contracts/events.md).

The four flows that used to be calls:

| Week 7 call | Week 8 event flow |
| --- | --- |
| Checkout, Inventory, Analytics `GET /items` from Catalog | Catalog publishes its fixed item list once at each start. Each consumer reads the topic from the beginning into an in-memory snapshot and is **ready** once it has seen the marker. A late or restarted service rebuilds from the topic; nobody calls Catalog. |
| Checkout `POST /internal/reservations` to Inventory | Checkout publishes `stock-reservation-requested`; Inventory reduces stock and publishes the outcome; Checkout finalizes or compensates. |
| Analytics `GET /internal/scans` from Checkout | A relay publishes each committed scan in order; Analytics consumes them, and after a restart replays the topic's tail. |
| Inventory and Analytics ask Catalog for names | They use their catalog snapshot. |

`/internal/reports/*` (read-only, for the verification tool) is the only HTTP a service serves
besides its public routes, and it is **blocked at the gateway**.

[`scripts/check_boundaries.py`](./scripts/check_boundaries.py) enforces this in the test suite.
Besides the Week 7 rules (no cross-service imports, no foreign tables, no shared stores) it fails on:
- **E1/E2** a peer's address in any service or in compose, or any HTTP client library, socket, or `*_URL` for another service;
- **E3–E6** a topic used but not declared, two producers for one topic, a consumer of a topic with no producer, or a service creating another's event types;
- **E7** two services on one Docker network;
- **E8** any `/internal/` route other than `/internal/reports/*`;
- **E9** an event type with no schema;
- **E10** the shared package knowing a service.

The only shared code is [`checkout_web`](./packages/checkout_web/): the contract's error shape,
request parsing, the app factory, `/healthz`, money, request ids, the **event envelope, the
`Bus` (Kafka and an in-memory twin for tests), the consumer loop and the catalog snapshot
reader**. It knows no schema and imports no database library.

## Completing a transaction: a saga over events

Completion is the one request that changes two stores, and no transaction spans two databases,
so Checkout **orchestrates** a short saga whose two calls are now events. Every step is
idempotent, so any step can be repeated safely, and the shopper still gets a synchronous answer:
the request waits (at most 3 s) for Inventory's reply event.

```mermaid
sequenceDiagram
    autonumber
    participant S as Shopper
    participant C as Checkout (checkout-db)
    participant K as Kafka
    participant I as Inventory (inventory-db)

    S->>C: POST /transactions/{id}/complete
    Note over C: ONE local tx: OPEN → COMPLETING,<br/>completion RESERVING (epoch n),<br/>request written to the outbox
    C->>K: stock-reservation-requested (key = tx id)
    K->>I: deliver
    Note over I: ONE local tx: lock stock rows in SKU order,<br/>check, decrement, alert, record APPLIED,<br/>outcome written to the outbox
    I->>K: stock-reserved
    K->>C: deliver to the waiting request
    Note over C: local tx: COMPLETING → COMPLETED
    C-->>S: 200 receipt
```

| What goes wrong | What happens | The shopper sees |
| --- | --- | --- |
| Stock is short | Inventory records `REJECTED` and replies `stock-rejected`; the transaction reopens | `409 INSUFFICIENT_STOCK` |
| The request or the reply is lost, or Inventory is slow | Checkout re-publishes the same request twice within its 3 s wait; Inventory answers a repeat from its record, never applying stock twice. Still silent: publish a **release**, reopen | `503`, transaction still open, retry works |
| The broker refuses the publish | The request is already committed in the outbox; the sweeper publishes it when the broker returns (and Inventory refuses it if the release overtook it) | `503` at the deadline, never a hang |
| Inventory is down | The request waits on the topic; Inventory handles it, then the release, when it returns | `503` now, retry works after |
| Checkout's own final step fails | **Release** the reservation, reopen | `503` |
| Checkout crashes mid-saga | The recovery loop (lease 10 s) rolls back an unconfirmed reserve, or finishes one that was recorded | after restart: `GET` shows `OPEN` or `COMPLETED` |
| A duplicate or late reply | Dropped: a waiter resolves once | unaffected |
| Analytics is down | Nobody waits for it | unaffected |

**No lost event.** Checkout and Inventory write each event into an **outbox** table in the same
local transaction as the state change, publish it right after the commit, and a sweeper
publishes anything left over (a crash between commit and publish, a broker outage). Checkout's
scan table is itself the outbox for scans. Delivery is at-least-once; every consumer is
idempotent: by `(transaction, epoch)` in Inventory, by scan id in Analytics, by SKU in
snapshots. **Ordering** comes from the key: all events about one transaction share a partition,
so a reserve is always seen before its release.

Full outcome table and timing budget:
[`completion-flow.md`](./specs/008-event-driven-kafka/contracts/completion-flow.md).

## Analytics: an in-memory pipeline fed by the scan topic

```text
KafkaScanSource ─▶ TailIngest ─q1─▶ WindowFilter ─q2─▶ AggregationFilter ─q3─▶ RankingFilter ─slot─▶ OutputFilter
 (scan events)    (by scan id)      (slide by id)      (running counts)         (top-200)              (publish)
```

The pipeline is Week 7's, unchanged; only its source changed.
- **KafkaScanSource** consumes `checkout.scans.v1` into a bounded buffer keyed by scan id. A scan id it has already seen is ignored, so the relay's at-least-once publishing counts each scan once.
- **Restart**: it replays only the last `2 × window` scans of the topic, enough to rebuild the current 1 000-scan window, **with no call to Checkout**. Scans made while Analytics was down are on the topic; it reads them when it returns.
- **Checkout is down**: Analytics keeps serving, and a restarted Analytics still rebuilds its window (Week 7 could not).
- **Checkout's data is reset**: the relay notices the scan log's identity changed and publishes `scan-log-reset`; Analytics clears its window and starts over.

## What changed from Week 7

| | Week 7 (007) | Week 8 (008) |
| --- | --- | --- |
| Service-to-service | HTTP calls on a `mesh` network | events on Kafka; no shared network between services |
| Price and name data | `GET /items` from Catalog | Catalog publishes once; every consumer keeps a snapshot built from the topic |
| Completion | Checkout `POST`s a reservation to Inventory | request and reply events, with an outbox on each side |
| Analytics feed | polls `GET /internal/scans` | consumes scan events; rebuilds from the topic without Checkout |
| Catalog down | a restarted Checkout could not price scans | a restarted Checkout rebuilds from the retained topic |
| Failure of one service | dependants fail cleanly | dependants fail cleanly, and work published meanwhile is processed when it returns |
| Dead letters | none | a poison event is set aside after five tries and never blocks a partition |

## Prerequisites

- **Docker Desktop**, running, with ≥ 8 CPUs and ≥ 8 GB memory (Settings → Resources), and Compose v2 (`docker compose version`).
- **JDK 21+** for the load client.
- **`uv`** and a native PostgreSQL 17 (`brew install postgresql@17`), only for the host-side test suite.

## Run

```bash
docker compose up -d --build --wait     # Kafka, topics, seeds, four services, gateway; blocks until healthy
curl -s localhost:8080/items | head -c 120
docker compose ps                       # 2 databases, kafka, 4 one-shot jobs (exited), 4 services, gateway: healthy
docker compose down -v                  # tear down, including every store and the broker's log
```

`--wait` returns only when the whole event path works: each service reports healthy once its
catalog snapshot has loaded and its consumers are positioned.

One service at a time:

```bash
docker compose stop inventory           # completions answer 503 (and stay retryable); low-stock answers 503; the rest work
docker compose start inventory          # it processes what arrived while it was down
docker compose stop kafka               # scans and reads work; completions answer 503 within 5 s; start it and they heal
docker compose up -d --build --no-deps --force-recreate analytics   # rebuild and redeploy just this one
```

Each seed job runs `--if-empty`, so it seeds only an empty store. A fresh run therefore always
starts with `docker compose down -v`.

## Test

```bash
uv sync
uv run pytest -q                        # all four services in-process on an in-memory bus with fault hooks
uv run python scripts/check_boundaries.py
docker compose up -d --build --wait     # the checks below need the stack
./scripts/check_isolation.sh            # no service can resolve a peer, or reach another's store; all reach Kafka
./scripts/check_no_peer_calls.sh        # a full workload through the gateway: no service ever connects to another
./scripts/trace_request.sh              # one request across the logs and the events that carry its id
./scripts/check_independence.sh         # recreate, then stop, each service (and Kafka); only dependants may fail
./scripts/check_crash_recovery.sh       # kill Checkout, then Inventory, under load; the stores must agree
./scripts/check_broker_outage.sh        # stop Kafka under load, start it; nothing lost, no dead letters
```

The suites in `tests/`:
- `contract/`: the frozen contract, unchanged from earlier weeks.
- `integration/`: catalog snapshots, completion events, the scan relay, Analytics' replay, Catalog-down behaviour, request ids, timeouts, the verification tool.
- `failure/`: every outcome of the completion flow (A–K), every event delivered 2× and 10×, an outbox crash between commit and publish, poison events, out-of-order delivery, Analytics faults, and many baskets racing for the hot SKU. After each, the stores must agree.
- `broker/`: the Kafka-specific parts on a **real broker** (topic config, keys landing on one partition, the compacted catalog topic, dead letters, replay). Run with `docker compose up -d kafka kafka-init --wait` then `BROKER_TESTS=1 uv run pytest tests/broker -q`.
- `unit/`: the envelope and schemas, the bus and consumer loop, the snapshot, filters, window, settings, routes, boundaries.

The in-process suite uses two native databases, `checkout_checkout_test` and
`checkout_inventory_test` (names must end in `_test` or it refuses to start), a temporary
catalog file, and an in-process bus whose hooks **drop, duplicate, delay and reorder** chosen
events, fail a publish, or lose an offset commit.

The same contract tests over real HTTP, through the gateway, the containers and the broker:

```bash
docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait
GATEWAY_URL=http://localhost:8080 \
CHECKOUT_TEST_DATABASE_URL=postgresql+asyncpg://checkout_user:checkout_pass@127.0.0.1:5442/checkout \
INVENTORY_TEST_DATABASE_URL=postgresql+asyncpg://inventory_user:inventory_pass@127.0.0.1:5443/inventory \
  uv run pytest -q tests/contract
docker compose -f compose.yaml -f compose.test.yaml down -v
```

## Measure

```bash
./scripts/run_load_tests.sh             # default run, then stress; fresh stack for each
./scripts/run_load_tests.sh stress      # or: default
docker compose --profile tools run --rm --build tools   # the graded correctness check, across all stores
python scripts/step_timings.py logs/compose-stress.log  # where a completion's time goes, by step
```

Each run does a fresh `down -v` and `up --wait`, then runs the load client with exactly the
assignment's flags (`java -cp out Main`, and `java -cp out Main --stations=100 --duration=120`,
through `./run.sh`). It moves the report into
`../load-client/reports/008-event-driven-kafka/`, saves the compose logs to `logs/`, verifies the
invariant, and leaves the last stack up for inspection.

The check opens no database: each service describes its own data over `/internal/reports/*`,
and the tool verifies that `initial_stock − current_stock` equals the completed line items, per
SKU; no stock is negative; no transaction is stuck `COMPLETING`; every reservation matches a
completed transaction; **no outbox row is waiting to be published; the scan relay has published
every scan;** and Analytics has read every scan.

### Graded reports

| Report | Config | tx/s | items/s | COMPLETE err % | Invariant |
| --- | --- | --- | --- | --- | --- |
| [`report-20261027-220244.json`](../load-client/reports/008-event-driven-kafka/report-20261027-220244.json) | default (`java -cp out Main`; 10 stations / 60 s) | 166.2 | 2 554 | 31.7 % | PASS |
| [`report-20261027-220513.json`](../load-client/reports/008-event-driven-kafka/report-20261027-220513.json) | stress (`--stations=100 --duration=120`) | 160.2 | 3 858 | 56.4 % | PASS |

Every error is a contract-defined `409 INSUFFICIENT_STOCK` once hot SKUs sell out (the logs show
exactly as many Inventory `stock-rejected` replies as errors); there were no `5xx` responses, no
timeouts, no re-publishes, no dead letters and no tracebacks.

**Against Week 7: 83 % (default) and 82 % (stress)**, above the 70 % the spec set. Scans and
starts are unchanged; a completion costs about two broker hops more (21.6 ms against 10.2 ms
at 10 stations), and at 100 stations Checkout's wait for the reply is mostly queueing for
Inventory's consumers (Inventory's own work is 10–20 ms of the ~150 ms). The breakdown, the
Week 7 comparison and the drills (including the broker-outage run that found and fixed a real
producer defect) are in
[`../load-client/reports/008-event-driven-kafka/README.md`](../load-client/reports/008-event-driven-kafka/README.md).
As in earlier weeks, results are not directly comparable to the native runs of Weeks 1–4: the
whole system runs inside Docker Desktop's VM.

## Inspect

Watch the events (Kafka's own console tools, inside the broker container):

```bash
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server kafka:9092 \
  --topic checkout.completions.v1 --from-beginning --max-messages 5 \
  --property print.headers=true --property print.key=true --property print.partition=true
docker compose exec kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --describe --group inventory                      # Inventory's lag: 0 on every partition when idle
```

Each database is reachable from the host on its published port (user and password are in
`.env.example`), or from inside its container:

```bash
psql -h 127.0.0.1 -p 5442 -U checkout_user checkout     # password: checkout_pass
psql -h 127.0.0.1 -p 5443 -U inventory_user inventory   # password: inventory_pass
```

Stock, in Inventory's database. `units_sold` should equal the `Units decremented` that the check
reports:

```sql
SELECT count(*)                                   AS skus,
       sum(initial_stock - current_stock)         AS units_sold,
       count(*) FILTER (WHERE current_stock = 0)  AS sold_out
  FROM inventory_stock;
```

Reservations, one per `(transaction, epoch)`: `APPLIED` ones belong to completed transactions,
`RELEASED` ones are rolled-back attempts, `REJECTED` ones were refused for stock (no units moved).
And the outbox: after a quiet moment every row is published.

```sql
SELECT state, count(*) FROM stock_adjustment GROUP BY state;
SELECT count(*) FILTER (WHERE published_at IS NULL) AS unpublished, count(*) AS total FROM outbox;
```

Completions in Checkout's database; after a quiet moment nothing should be `RESERVING`,
`RESERVED` or `COMPENSATING`, and the relay's position should equal the highest scan id:

```sql
SELECT state, count(*) FROM completion GROUP BY state;
SELECT (SELECT last_published_scan_id FROM relay_state) AS relayed, (SELECT max(id) FROM transaction_item) AS held;
```

What Analytics is serving, and how far it has read:

```bash
curl -s 'localhost:8080/analytics/popular-items?limit=5'
curl -s localhost:8084/internal/reports/cursor        # {"lastScanId": ..., "highestApplied": ..., "windowEnd": ...}
```

Trace one request across the gateway, Checkout, Kafka and Inventory:

```bash
./scripts/trace_request.sh                 # shops once, then prints the log lines and the events carrying the id
```

## How it works

| Concern | Approach | Where |
| --- | --- | --- |
| Routing | nginx, by path: `= /items` → catalog, `/transactions` → checkout, `/inventory/` → inventory, `/analytics/` → analytics, `/internal/` → 404, everything else → checkout. Stamps `X-Request-ID` | `gateway/nginx.conf` |
| Catalog | A CSV loaded into memory; `GET /items` is a pre-rendered body. At startup its items and a marker are published to a compacted topic | `catalog_service/store.py`, `publisher.py` |
| Scan | One CTE: bump the transaction's counters, insert the line (priced from the snapshot), `RETURNING` both. Autocommit, one round trip; no event on the request path | `checkout_service/service.py` → `repo.py` |
| Scan → Analytics | A leader-elected relay (a PostgreSQL advisory lock) publishes committed scans in id order, waiting out sequence holes | `checkout_service/relay.py` |
| Completion | The saga above | `checkout_service/saga.py`, `stock_events.py`, `recovery.py`; `inventory_service/handlers.py`, `reserve.py` |
| No lost event | Transactional outbox + fast-path publish + sweeper, both sides | `checkout_service/outbox.py`, `inventory_service/outbox.py` |
| Never oversell | Inventory's conditional statement locks the stock rows in SKU order, checks, decrements and records the reservation at once; `CHECK (current_stock >= 0)` as a backstop | `inventory_service/reserve.py` |
| Poison events | five tries, then `dead-letters.v1`; an outage of the database or the broker is waited out | `checkout_web/consumer_loop.py` |
| Low-stock report | Live stock joined to the latest alert per SKU; names from the catalog snapshot | `inventory_service/low_stock.py` |
| Popular items | In-memory pipeline fed by the scan topic | `analytics_service/pipeline.py`, `scan_source.py`, `popular_items.py` |
| Money | Integer cents end to end | `checkout_web/money.py` |

## Layout

```
compose.yaml            checkout-db, inventory-db, kafka + kafka-init, 3 seed jobs, catalog, checkout,
                        inventory, analytics, gateway, tools; networks edge_<svc>, bus_<svc>, one per database
compose.test.yaml       test-stack override (small catalog, separate project and images)
docs/                   architecture.md (+ architecture.svg): the diagram, topics, flows
events/                 topics.yaml (broker topology), create-topics.sh (generated), schemas/ (one per event type)
gateway/                nginx.conf + Dockerfile: routing only
packages/
  checkout_web/         SHARED, DATA-FREE: errors, request helpers and ids, app factory, /healthz,
                        money, event envelope, Bus (Kafka + in-memory), consumer loop, catalog snapshot
services/
  catalog/              catalog_service    store (CSV), publisher, seed
  checkout/             checkout_service   service, saga, stock_events, outbox, relay, recovery, repo, models, seed
  inventory/            inventory_service  handlers, reserve, outbox, low_stock, repo, models, seed
  analytics/            analytics_service  pipeline, supervisor, scan_source, scan_window, state
tools/                  verify_invariant.py + the checkout-tools Dockerfile
scripts/
  run_load_tests.sh     fresh stack → load → verify, for the default and stress runs
  check_boundaries.py   the service-boundary and event-topology rules
  check_isolation.sh    no service can resolve a peer or reach another's store; all reach Kafka
  check_no_peer_calls.sh    a workload through the gateway; no service ever connects to another
  check_independence.sh recreate, then stop, each service and Kafka; only dependants may fail
  check_crash_recovery.sh   kill Checkout, then Inventory, under load; the stores must agree
  check_broker_outage.sh    stop Kafka under load; nothing lost, no dead letters
  trace_request.sh      one request id across the logs and the events
  create_topics.py      events/create-topics.sh from events/topics.yaml
  step_timings.py       per-step timings from the logs
  lock_services.sh      regenerate the per-image requirements.lock files
tests/                  contract / integration / failure / broker / unit; _dispatch.py is the gateway,
                        in-process; conftest.py wires the four services onto one bus
```

## Design artifacts

- Week 8 (this architecture): [`specs/008-event-driven-kafka/`](./specs/008-event-driven-kafka/):
  - [spec](./specs/008-event-driven-kafka/spec.md)
  - [plan](./specs/008-event-driven-kafka/plan.md)
  - [research](./specs/008-event-driven-kafka/research.md)
  - [data model](./specs/008-event-driven-kafka/data-model.md)
  - [contracts](./specs/008-event-driven-kafka/contracts/): events, completion flow, compose topology, service boundaries, architecture diagram
  - [quickstart](./specs/008-event-driven-kafka/quickstart.md)
  - [tasks](./specs/008-event-driven-kafka/tasks.md)
- Week 7 (database per service, HTTP between services): [`specs/007-microservices-architecture/`](./specs/007-microservices-architecture/)
- Week 6 (hot-path latency): [`specs/006-service-perf-investigation/`](./specs/006-service-perf-investigation/)
- Week 5 (services over one shared database): [`specs/005-service-architecture/`](./specs/005-service-architecture/)
- Week 4 (streaming pipeline): [`specs/004-streaming-analytics/`](./specs/004-streaming-analytics/)
- Week 3 (pipelined analytics): [`specs/003-pipelined-analytics/`](./specs/003-pipelined-analytics/)
- Week 2 (layering): [`specs/002-layered-architecture/`](./specs/002-layered-architecture/)
- Week 1 (behaviour, schema, contract; still authoritative): [`specs/001-checkout-backend/`](./specs/001-checkout-backend/)
