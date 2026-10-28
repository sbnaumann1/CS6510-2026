# Implementation Plan: Event-Driven Microservice Communication (Kafka Event Broker)

**Branch**: `008-event-driven-kafka` | **Date**: 2026-10-26 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/008-event-driven-kafka/spec.md`

## Summary

Spec 007's four microservices keep their private stores and the gateway keeps the public routes.
What changes is the **mesh**: every service-to-service HTTP call is replaced by events on one
single-broker **Apache Kafka** (KRaft) deployment, and the `mesh` network disappears. After this
feature no service holds another service's address.

Four calls become four event flows ([research R1–R8](./research.md), [contracts/events.md](./contracts/events.md)):

1. **Catalog → everyone (reference data).** Catalog's items never change, so Catalog publishes the
   whole list **once at startup** to a log-compacted topic, followed by a "catalog published"
   marker. Checkout, Inventory and Analytics each build an in-memory snapshot by reading the topic
   from the beginning, and are *ready* once they have seen the marker. A late or restarted consumer
   rebuilds from the topic; nobody calls Catalog.
2. **Checkout → Inventory → Checkout (completion).** The 007 orchestrated saga keeps its states,
   epochs, lease and recovery loop, but its two wire calls become events: Checkout publishes
   `stock-reservation-requested` / `stock-release-requested`; Inventory applies them in one local
   transaction (unchanged idempotent `reserve`/`release` logic) and publishes the outcome; Checkout
   awaits the outcome event, then finalizes or compensates. The shopper's `complete` still returns
   a synchronous answer by awaiting the outcome event for a bounded time. See
   [contracts/completion-flow.md](./contracts/completion-flow.md).
3. **Checkout → Analytics (scans).** Checkout's scan table is its own outbox: a leader-elected
   relay publishes scans in id order to a single-partition topic. Analytics consumes them in order,
   replaying the last window's worth after a restart. The 007 HTTP pull and its `/internal/scans`
   endpoint are deleted.
4. **State change + event never diverge (FR-012).** Completion and outcome events are written to a
   per-service **transactional outbox** in the same local transaction as the state change, then
   published immediately after commit (fast path) with a sweeper as the safety net. Consumers are
   idempotent (007's per-`(transaction, epoch)` records already are), so at-least-once delivery
   is safe.

Isolation becomes structural: each service sits on its own `edge_<svc>` network and its own
`bus_<svc>` network, Kafka on all `bus_*` networks, so **no two services share a network**. The
automated drill is "can a container resolve or reach a peer?".

## Technical Context

**Language/Version**: Python 3.11 (unchanged)

**Primary Dependencies**: FastAPI + uvicorn, SQLAlchemy 2 async + asyncpg, orjson (unchanged); **aiokafka** added to every service image (`httpx` removed from the four services; the tools image keeps it for operator checks); nginx gateway (unchanged)

**Broker**: Apache Kafka 3.9 (`apache/kafka`, KRaft, one broker, one container). Topics created by a one-shot `kafka-init` job from [events/topics.yaml](./contracts/events.md). JSON events with a documented envelope and JSON Schemas; no schema registry.

**Storage**: unchanged from 007 (`checkout-db`, `inventory-db` PostgreSQL; Catalog read-only file; Analytics in memory). New tables: `outbox` and `relay_state` in Checkout; `outbox` in Inventory; a `REJECTED` state in Inventory's adjustment table. Kafka's own log is the retained history for snapshots and Analytics.

**Testing**: pytest + pytest-asyncio as in 007. Services talk through a small `Bus` interface with two implementations: `KafkaBus` and an in-process `MemoryBus` that can drop, duplicate, delay and reorder chosen events, replacing 007's fault-injecting ASGI mesh for deterministic failure tests. A broker-backed integration subset and the compose drills run against real Kafka.

**Target Platform**: Docker Compose on Docker Desktop (macOS), as in 007

**Project Type**: multi-service web backend (four services + gateway + Kafka + one-shot seeds and topic init)

**Performance Goals**: stress run (100 stations, 120 s) reported beside 007's 196.2 tx/s and default run beside 200.5 tx/s; target ≥ 70 % of 007, explain any shortfall (SC-010). Analytics catches up within 30 s of recovery (SC-008).

**Constraints**: contract unchanged; stock never negative; no service-to-service HTTP; every consumer idempotent; every awaited outcome has a deadline (4 s); at most one hot-SKU lock hold per statement (spec 006 finding, unchanged)

**Scale/Scope**: 4 services, 1 broker, 5 topics, 6 event types, 2 databases; ~2 000 items; 100 stations

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template, so there are no ratified
principles. As in 007, the plan holds itself to the standards the earlier specs recorded:

| Standard (source) | Status |
| --- | --- |
| Frozen external contract and unmodified load client (001, 007 FR-001) | PASS: public routes unchanged; `/internal/reservations` and `/internal/scans` are removed, not exposed |
| Invariant: `initial_stock − final_stock == completed line items`, never negative (001) | PASS: kept; `tools/verify_invariant.py` extended to check Analytics has consumed every scan and no outbox row is unpublished |
| Boundary rules enforced by an automated check (005, 007) | PASS: `check_boundaries.py` gains rules for no peer URLs, no `httpx` in services, topic ownership ([contracts/service-boundaries.md](./contracts/service-boundaries.md)) |
| Spec 006 hot-SKU finding: never hold the stock lock across round trips | PASS: Inventory still takes it for one statement plus COMMIT; Kafka I/O happens after COMMIT |
| 007 FR-005: shared code is data-free | PASS: the shared `Bus`, envelope and topic constants hold no data access; each service owns its outbox tables and relay |

Post-design re-check: PASS. One justified addition is recorded in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/008-event-driven-kafka/
├── plan.md                          # This file
├── research.md                      # Phase 0: decisions with rationale and alternatives
├── data-model.md                    # Phase 1: events, outbox tables, state changes
├── quickstart.md                    # Phase 1: validation scenarios and deliverables
├── contracts/
│   ├── events.md                    # topics, keys, envelope, every event payload, producers/consumers
│   ├── completion-flow.md           # saga over events: sequence, outcomes, recovery, timing
│   ├── compose-topology.md          # Kafka, topic init, per-service networks, startup order
│   ├── service-boundaries.md        # rules enforced by scripts/check_boundaries.py
│   └── architecture-diagram.md      # what the committed diagram must show (FR-023)
├── checklists/requirements.md
└── tasks.md                         # Phase 2 (/speckit-tasks, not created here)
```

### Source Code (backend/)

```text
backend/
├── compose.yaml                     # + kafka, kafka-init; `mesh` removed; edge_<svc> and bus_<svc> per service
├── compose.test.yaml                # + a Kafka for the broker-backed test subset
├── events/
│   ├── topics.yaml                  # topic name, partitions, retention/compaction, producer, consumers
│   └── schemas/                     # one JSON Schema per event type (envelope + payload), versioned
├── gateway/                         # unchanged
├── packages/checkout_web/           # data-free only; client.py (httpx) DELETED
│   └── src/checkout_web/
│       ├── events.py                # envelope, event types, topic constants, (de)serialization
│       ├── bus.py                   # Bus protocol; KafkaBus (aiokafka); MemoryBus with fault hooks
│       ├── consumer_loop.py         # poll → handle → commit-after-success, retries, dead-letter
│       ├── snapshot.py              # generic reader of catalog.items.v1 (ready after the marker)
│       └── lifecycle.py             # cancel_and_wait: shutdown that survives a lost cancellation
├── services/
│   ├── catalog/src/catalog_service/
│   │   ├── …                        # api, store, seed unchanged
│   │   └── publisher.py             # publish items + marker at startup, retry until the broker is up
│   ├── checkout/src/checkout_service/
│   │   ├── saga.py                  # same states; reserve/release now publish and await outcome
│   │   ├── stock_events.py          # replaces inventory_client.py: request/await-outcome over the Bus
│   │   ├── outbox.py  relay.py      # outbox table, fast-path publish, sweeper, scan relay (leader)
│   │   ├── catalog_snapshot.py      # replaces catalog_client.py: snapshot from the catalog topic
│   │   ├── recovery.py              # same loop; steps use stock_events
│   │   └── (internal_api.py DELETED; internal_reports.py kept for the operator check only)
│   ├── inventory/src/inventory_service/
│   │   ├── handlers.py              # consume reservation/release requests → reserve/release → outbox
│   │   ├── outbox.py                # outbox table, fast-path publish, sweeper
│   │   ├── catalog_snapshot.py      # item names from the catalog topic
│   │   └── (internal_api.py, catalog_client.py DELETED)
│   └── analytics/src/analytics_service/
│       ├── scan_source.py           # KafkaScanSource implements the existing ScanSource seam
│       ├── catalog_snapshot.py      # item names from the catalog topic
│       └── (HttpScanSource, catalog_client.py DELETED; pipeline reused)
├── tools/verify_invariant.py        # + unpublished-outbox and Analytics-consumed-every-scan checks
├── scripts/
│   ├── check_boundaries.py          # + event rules
│   ├── check_isolation.sh           # no container can resolve or reach a peer service
│   ├── check_no_peer_calls.sh       # compose-level: block everything but the broker, run the workflow
│   ├── check_independence.sh        # stop each service in turn; only dependants may fail
│   ├── check_broker_outage.sh       # stop Kafka mid-load, restart, verify the invariant
│   ├── check_crash_recovery.sh      # kill Checkout / Inventory under load, verify the invariant
│   ├── trace_request.sh             # one correlation id across logs and event headers
│   ├── run_load_tests.sh            # REPORT_DIR default 008-event-driven-kafka
│   └── render_diagram.sh            # Mermaid → SVG for the committed diagram
├── docs/
│   └── architecture.md              # the architecture diagram (Mermaid, plus rendered SVG)
└── tests/
    ├── contract/                    # assertions unchanged
    ├── integration/                 # existing + catalog snapshot, scan relay, Analytics replay
    ├── failure/                     # per-step fault injection via MemoryBus (duplicate, drop, delay, reorder)
    ├── broker/                      # real-Kafka subset: topics, snapshot, completion, DLQ
    ├── unit/
    ├── _dispatch.py                 # gateway twin only; mesh twin removed
    └── conftest.py                  # MemoryBus by default; Kafka when BROKER_TESTS=1
```

Load reports go to `load-client/reports/008-event-driven-kafka/` (two JSON files).

**Structure Decision**: Keep 007's per-service layout and its shared data-free `checkout_web`. Each
`*_client.py` (HTTP) is replaced by an event-based module with the **same seam** (`stock_events`
for `inventory_client`, `catalog_snapshot` for `catalog_client`, `KafkaScanSource` for
`HttpScanSource`), so the saga, the recovery loop and the analytics pipeline keep their logic and
their tests, and only transports change. This is the smallest change that makes the broker the
only path between services.

## Complexity Tracking

No constitution violations. Two additions carry real weight and are required by the spec:

| Addition | Why Needed | Simpler Alternative Rejected Because |
| --- | --- | --- |
| Transactional outbox + fast-path publish + sweeper (Checkout, Inventory) | FR-012: a state change and its event must not diverge across a crash | Publishing after commit with no record loses the event on a crash; publishing before commit announces something that may roll back |
| Scan relay with a leader and gap grace in Checkout | FR-007, FR-012, FR-016: every scan must reach Analytics exactly once in effect, in order | Publishing from the request handler after COMMIT loses scans on a crash; a per-scan outbox write costs the hot path (spec 006) |
