---
description: "Task list for 008 event-driven microservice communication over Kafka"
---

# Tasks: Event-Driven Microservice Communication (Kafka Event Broker)

**Input**: Design documents from `specs/008-event-driven-kafka/`
**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: **Included.** The spec requires them: duplicate, out-of-order, crash, broker-outage and stopped-service tests (FR-020), the boundary and isolation checks (FR-019, SC-002), the cross-store invariant (FR-021) and the unchanged contract suite (SC-001).

**Paths**: Relative to `backend/` unless they start with `../` (the repo root, e.g. `../load-client/`). Python 3.11. Run commands from `backend/`.

**Ground rules for every task**:
- **Change the transport, not the logic**: the saga states, epochs, lease, recovery loop, the tuned SQL (spec 006) and the analytics pipeline are 007 code. When a task says "replace the client" it keeps the module's public seam (return types, method names) so callers change only their imports. Do not rewrite or "improve" SQL.
- **Events are the only path between services**: after US2 no service imports `httpx` or holds a peer URL. Until then, old and new paths may coexist, but new code never adds a peer call.
- **State change and event commit together**: every handler that changes state and emits an event writes the outbox row in the same local transaction, then publishes after COMMIT ([research R7](./research.md)). Offsets are committed after the local transaction commits.
- **Idempotent consumers**: key by `(transaction, epoch)`, `scanId` or SKU ([contracts/events.md](./contracts/events.md) "Guarantees and rules").
- **Contract tests**: assertions in `tests/contract/` are not edited. Fixtures may change.
- **Schema changes need a fresh stack**: new tables and the `REJECTED` state arrive via `create_all` in each service's seed; use `docker compose down -v` before testing against compose.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US5 from spec.md

---

## Phase 1: Setup

**Purpose**: Dependencies, topology file and empty modules so later tasks have somewhere to land.

- [ ] T001 Update `packages/checkout_web/pyproject.toml`: bump `version` to `0.8.0`, add `aiokafka` (pin the newest release at least two weeks old and record it) and `jsonschema` as a **dev-group** dependency in the root `pyproject.toml` (schemas are validated in tests only). Keep `httpx` for now; T041 removes it.
- [ ] T002 [P] Bump `version` to `0.8.0` in `services/{catalog,checkout,inventory,analytics}/pyproject.toml` and `checkout-web==0.8.0` in each; add `aiokafka` (same pin as T001) to each service's dependencies.
- [ ] T003 Run `uv lock && uv sync`, then `scripts/lock_services.sh` to regenerate the four `services/*/requirements.lock` files. Confirm `uv run python -c "import aiokafka, checkout_web, catalog_service, checkout_service, inventory_service, analytics_service"` succeeds.
- [ ] T004 Create the empty module files named in [plan.md](./plan.md) Project Structure, each with only a module docstring naming its purpose: `packages/checkout_web/src/checkout_web/{events,bus,snapshot,consumer_loop}.py`; `services/catalog/src/catalog_service/{publisher,events}.py`; `services/checkout/src/checkout_service/{stock_events,outbox,relay,catalog_snapshot,events}.py`; `services/inventory/src/inventory_service/{handlers,outbox,catalog_snapshot,events}.py`; `services/analytics/src/analytics_service/{catalog_snapshot,events}.py`; and the directories `events/schemas/`, `docs/`, `tests/broker/`.

---

## Phase 2: Foundational (blocking prerequisites for every story)

**Purpose**: The event envelope, the bus (Kafka and in-memory), the consumer loop, the topic manifest and schemas, and the broker in compose. Everything in the user stories builds on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [ ] T005 Write `events/topics.yaml` from [contracts/events.md](./contracts/events.md) "Topics": for each of `catalog.items.v1` (1 partition, `cleanup.policy=compact`), `checkout.scans.v1` (1 partition, 7 d), `checkout.completions.v1` (6 partitions, 1 d), `inventory.stock.v1` (6 partitions, 1 d), `dead-letters.v1` (1 partition, 14 d) give `partitions`, `retention_ms` or `compact`, `producer` (service name; `*` for dead letters), `consumers` (list of services), and `key` (what the key is).
- [ ] T006 [P] Write one JSON Schema per event type in `events/schemas/`: `envelope.json` (required `eventId`, `type`, `version`, `correlationId`, `occurredAt`, `producer`, `data`; `eventId` a UUID; `version` an integer) and one file per payload in [contracts/events.md](./contracts/events.md): `catalog.item-published.json`, `catalog.catalog-published.json`, `checkout.scan-recorded.json`, `checkout.scan-log-reset.json`, `checkout.stock-reservation-requested.json`, `checkout.stock-release-requested.json`, `inventory.stock-reserved.json`, `inventory.stock-rejected.json`, `inventory.stock-released.json`, `inventory.reservation-refused.json`, `dead-letter.json`. Payload fields and types exactly as in that contract; payloads allow additional properties (compatibility rule).
- [ ] T007 Implement `packages/checkout_web/src/checkout_web/events.py`: topic-name constants; event-type constants; frozen dataclass `Event` (envelope fields); `make_event(type, producer, data, *, correlation_id=None, event_id=None)` that takes the correlation id from the current request id (`checkout_web/request.py`) when not given; `encode(event) -> bytes` and `decode(bytes) -> Event` using `orjson`, raising `EventDecodeError` for malformed input or an unknown **major** `version`; `headers(event)` returning `x-request-id` and `x-event-type`. No database imports.
- [ ] T008 Implement `packages/checkout_web/src/checkout_web/bus.py`: `Bus` protocol with `start()`, `stop()`, `publish(topic, event, key=None)` (returns after the broker acknowledges), and consumer factories `group_consumer(topic, group)` and `assigned_consumer(topic, *, from_beginning: bool)` returning an async-iterable of `Record(topic, partition, offset, key, event_or_raw)` with `commit()` and `seek_*`. Implement `KafkaBus` on aiokafka with `acks="all"`, `enable_idempotence=True`, `linger_ms=2`, consumers with `enable_auto_commit=False`, bootstrap from `KAFKA_BOOTSTRAP`, and start-up retry with backoff so a service can start before the broker. Expose `ready()` (producer started and consumers positioned).
- [ ] T009 [P] Implement `MemoryBus` in `packages/checkout_web/src/checkout_web/bus.py` (same module, class beneath `KafkaBus`) with the same interface: topics with N partitions chosen by key hash, per-partition offsets, committed offsets per group, compaction for `catalog.items.v1`-style topics, and **fault hooks** used by tests: `drop(predicate, times)`, `duplicate(predicate, times)`, `delay(predicate, seconds)`, `reorder(topic)`, `fail_publish(times)`, and `crash_before_commit(group, times)`. One shared `MemoryBus` instance joins the four in-process service apps.
- [ ] T010 Implement `packages/checkout_web/src/checkout_web/consumer_loop.py`: `run_consumer(consumer, handler, *, name, bus, max_attempts=5, backoff_s=0.2)` that polls, calls `handler(records)` (a batch), commits offsets only after the handler returns, retries a failing record with doubling backoff, and after the fifth failure publishes a `dead-letter` event (payload per [contracts/events.md](./contracts/events.md) "Dead letters") and commits so later records proceed. Transient infrastructure errors (an exception type `Transient` raised by handlers for database or broker unreachable) pause and retry **without** counting toward dead-lettering. Sets the request id from the record's `x-request-id` header around each handler call.
- [ ] T011 [P] Implement `packages/checkout_web/src/checkout_web/snapshot.py`: generic `TopicSnapshot` that reads a compacted topic from the beginning with an `assigned_consumer`, applies each `catalog.item-published` event to a dict (overwrite by SKU), records the latest `catalog.catalog-published` marker, exposes `ready` (marker seen and `len(items) == itemCount`), `get(sku)`, `__contains__`, and `wait_ready(timeout)`, and keeps running so a republished catalog replaces the contents. Data-free: it knows nothing about prices or names beyond the dict values it is handed through a `parse(data)` callback.
- [ ] T012 [P] Add readiness hooks to `packages/checkout_web/src/checkout_web/health.py` and `app.py`: `create_service_app(..., readiness=[callable, ...])`; `/healthz` returns `200` only when the database ping (if any) and every readiness callable return true, otherwise `503` with the failing check's name.
- [ ] T013 [P] Per-service `events.py` declaring `PRODUCES` and `CONSUMES` lists of topic names exactly as in [contracts/events.md](./contracts/events.md): `services/catalog/src/catalog_service/events.py` (produces `catalog.items.v1`; consumes nothing), `services/checkout/src/checkout_service/events.py` (produces `checkout.scans.v1`, `checkout.completions.v1`; consumes `catalog.items.v1`, `inventory.stock.v1`), `services/inventory/src/inventory_service/events.py` (produces `inventory.stock.v1`; consumes `checkout.completions.v1`, `catalog.items.v1`), `services/analytics/src/analytics_service/events.py` (produces nothing; consumes `checkout.scans.v1`, `catalog.items.v1`). Each service also lists `dead-letters.v1` as produced (consumers publish dead letters).
- [ ] T014 [P] Add `kafka_bootstrap` (`KAFKA_BOOTSTRAP`, default `kafka:9092`) to each service's `config.py` (`services/*/src/*_service/config.py`). Do not remove peer URLs yet (T042).
- [ ] T015 Update `compose.yaml`: add volume `kafka_data`; add `kafka` (image `apache/kafka`, a pinned 3.9.x tag, KRaft combined mode with node id 1, one listener `kafka:9092` for containers plus `127.0.0.1:9094` for the host, `KAFKA_HEAP_OPTS=-Xmx512m`, single-node replication factors of 1, health check listing topics with `/opt/kafka/bin/kafka-topics.sh`) and `kafka-init` (a one-shot job that creates every topic in `events/topics.yaml` with `--if-not-exists`, exits `0`; build its command from the manifest by a small script `scripts/create_topics.py`). Networks stay as in 007 for now; T043 restructures them. Mirror a Kafka service in `compose.test.yaml` on a separate host port for `tests/broker/`.
- [ ] T016 [P] Unit tests `tests/unit/test_events.py`: every schema in `events/schemas/` validates a sample payload built by `make_event`; `decode(encode(e)) == e`; malformed bytes and an unknown major version raise `EventDecodeError`; unknown extra fields are accepted.
- [ ] T017 [P] Unit tests `tests/unit/test_memory_bus.py` and `tests/unit/test_consumer_loop.py`: same-key records stay in order within a partition; committed offsets resume after a simulated restart; each fault hook behaves as named; a handler that fails once is retried; five failures produce a dead-letter event and the next record is processed; a `Transient` exception is retried without a dead letter.
- [ ] T018 [P] Unit test `tests/unit/test_snapshot.py`: not ready before the marker; ready once marker and `itemCount` items are read; an unknown SKU returns `None` only when ready; a second `catalog-published` replaces the content; duplicate item events are harmless.
- [ ] T019 Update `tests/conftest.py`: add a `bus` fixture that is a `MemoryBus` by default and a `KafkaBus` against `compose.test.yaml`'s broker when `BROKER_TESTS=1`; keep the two-database and temp-catalog fixtures from 007. Do not change `tests/_dispatch.py` yet (T047).

**Checkpoint**: `uv run pytest tests/unit -q` passes; `docker compose up -d kafka kafka-init --wait` creates the five topics.

---

## Phase 3: User Story 1 - Unchanged checkout experience over events (Priority: P1) 🎯 MVP

**Goal**: The shopper-facing behavior is identical while Catalog data, completions and scans move over Kafka.

**Independent Test**: With the stack up, the existing contract and integration tests and the unmodified load client pass against the gateway with no assertion changes ([quickstart.md](./quickstart.md) step 2).

### Catalog flow: reference data by event

- [ ] T020 [US1] Implement `services/catalog/src/catalog_service/publisher.py`: on startup read all items from the store, publish one `catalog.item-published` per item (key = SKU; `{"sku", "name", "priceCents"}`) and then one `catalog.catalog-published` (key `__marker__`; `{"publishId": <uuid>, "itemCount": N}`). Retry with backoff until the broker accepts everything. Wire it into `services/catalog/src/catalog_service/main.py` as a background task started by the lifespan; it republishes on every process start, never otherwise.
- [ ] T021 [US1] Implement `services/checkout/src/checkout_service/catalog_snapshot.py` on `checkout_web.snapshot.TopicSnapshot`: `get(sku) -> (name, price_cents) | None` (raises `UpstreamUnavailable` while not ready, giving a clean `503` for a cold start), `name_of(sku)` (falls back to the SKU), `get_snapshot()`, `reset()`. Update `service.py` and `saga.py` to import it instead of `catalog_client`, and `main.py` to start it with the app and add it to `readiness`. Keep `catalog_client.py` until T041.
- [ ] T022 [P] [US1] Implement `services/inventory/src/inventory_service/catalog_snapshot.py` (same base class) exposing `names(skus) -> dict[str, str]` and `get_snapshot()`; update `low_stock.py` and `main.py` (readiness) to use it.
- [ ] T023 [P] [US1] Implement `services/analytics/src/analytics_service/catalog_snapshot.py` exposing `names(skus)` and `get_snapshot()`; update `popular_items.py` and `main.py` (readiness) to use it.

### Completion flow: saga over events

- [ ] T024 [US1] Checkout outbox storage. In `services/checkout/src/checkout_service/models.py` add table `outbox` with columns `id` (bigint identity PK), `topic` (text), `key` (text), `event_id` (uuid, unique), `payload` (jsonb), `created_at` (timestamptz), `published_at` (timestamptz null) and a partial index on `(id) WHERE published_at IS NULL`; add table `relay_state` with one row, `last_published_scan_id` (bigint). In `repo.py` add `insert_outbox(session, topic, key, event)`, `mark_published(event_id)`, `claim_unpublished(older_than_s=2, limit=100)` using `FOR UPDATE SKIP LOCKED`, and `delete_published_older_than(hours=1)`. Ensure `seed.py` creates both tables.
- [ ] T025 [US1] Implement `services/checkout/src/checkout_service/outbox.py`: `publish_after_commit(event_ids)` (fast path: publish rows just committed, then `mark_published`), and `sweeper_loop()` (every 1 s republish rows unpublished after 2 s, delete published rows older than 1 hour; safe in every worker because publishing twice is harmless). Start the sweeper in `main.py`'s lifespan with the other tasks.
- [ ] T026 [US1] Implement `services/checkout/src/checkout_service/stock_events.py` per [research R5](./research.md): one reply consumer per worker process using `assigned_consumer("inventory.stock.v1", from_beginning=False)` over all partitions; `reserve(public_id, epoch, lines)` and `release(public_id, epoch)` with the **same return types as `inventory_client`** (`Applied`, `Insufficient(short_skus)`, `ReservationReleased`; `release` returns `None`) so `saga.py` is unchanged apart from imports; register the waiter keyed `(transaction, epoch)` **before** publishing; wait 1.5 s, re-publish, 1.5 s, re-publish, 1.0 s (4 s total) then raise `UpstreamUnavailable("inventory", …)`; `release` waits 2 s; resolve each waiter at most once and drop outcomes with no waiter (log `ignored-stale`); expose `ready()` (assigned and positioned) for `/healthz`.
- [ ] T027 [US1] Update `services/checkout/src/checkout_service/saga.py` and `repo.py` so step 1 (`mark_completing`) writes the `checkout.stock-reservation-requested` outbox row **in the same local transaction** (payload per [contracts/events.md](./contracts/events.md): `transactionId`, `epoch`, `lines` merged by SKU and sorted by SKU), and `rollback` writes the `checkout.stock-release-requested` row (`transactionId`, `epoch`) in the `COMPENSATING` transition's transaction. `stock_events.reserve/release` then call `outbox.publish_after_commit` for those rows instead of building the events themselves. Keep every state, epoch, lease and error-precedence rule of [completion-flow.md](./contracts/completion-flow.md); log `publish`, `await` steps per its "Logging" section.
- [ ] T028 [US1] Update `services/checkout/src/checkout_service/recovery.py` to use `stock_events` through `saga.rollback` / `saga.finalize_step` (no direct client call remains); behavior per the "Recovery loop" table in [completion-flow.md](./contracts/completion-flow.md).
- [ ] T029 [US1] Inventory storage. In `services/inventory/src/inventory_service/models.py` change the `stock_adjustment` check constraint to `state IN ('APPLIED', 'RELEASED', 'REJECTED')`, add table `outbox` (same columns and partial index as T024, `topic = inventory.stock.v1`), and add repo functions in `repo.py` mirroring T024's. Ensure `seed.py` creates the table. The invariant checker must keep counting only `APPLIED` rows.
- [ ] T030 [US1] Update `services/inventory/src/inventory_service/reserve.py`: `reserve_once` returns a new `Rejected(skus)` result **recorded** as a `REJECTED` adjustment (no stock change); a repeated request for a recorded `(transaction, epoch)` replays its recorded outcome (`APPLIED`, `RELEASED`→`reservation-refused`, `REJECTED`→`stock-rejected`); every path writes the matching outcome event to the outbox **in the same local transaction** (`inventory.stock-reserved`, `inventory.stock-rejected`, `inventory.stock-released`, `inventory.reservation-refused`, each with `inReplyTo` = the request's `eventId`). `release` likewise emits `stock-released` (`alreadyReleased` true on repeats). Keep the SQL statements and lock order unchanged.
- [ ] T031 [US1] Implement `services/inventory/src/inventory_service/outbox.py` (fast-path publish and sweeper, as T025) and `handlers.py`: consume `checkout.completions.v1` in group `inventory` via `consumer_loop`; per fetched batch, run handlers concurrently across distinct transaction keys and sequentially within a key (bounded by the pool size), call `reserve_once` or `release` with `settings.low_stock_threshold`, publish after commit, then commit offsets. Wire into `main.py`'s lifespan; readiness = consumer assigned. Keep `internal_api.py` until T041.

### Scan flow: Analytics fed by events

- [ ] T032 [US1] Implement `services/checkout/src/checkout_service/relay.py` per [research R8](./research.md): acquire a PostgreSQL advisory lock (`pg_try_advisory_lock`) in Checkout's own database, otherwise retry every second; read `relay_state.last_published_scan_id`; select `scan` rows with `id >` it in order; publish the **contiguous prefix** as `checkout.scan-recorded` (`{"scanId", "transactionId", "sku"}`, no key); treat a missing id as a gap and skip it only after `popular_gap_grace_ms`-equivalent (`RELAY_GAP_GRACE_MS`, default 1000); save the position **after** the broker acknowledges a batch; if `scan_log.token` differs from the token recorded in `relay_state`, publish `checkout.scan-log-reset` (`{"logId"}`), reset the position and continue. Start it in `main.py`'s lifespan.
- [ ] T033 [US1] Implement `KafkaScanSource` in `services/analytics/src/analytics_service/scan_source.py` behind the existing `ScanSource` protocol: on start seek `checkout.scans.v1` to `end − 2 × popular_window_size` (or the beginning if shorter) and read forward; ignore any `scanId` ≤ the highest applied; clear state on `checkout.scan-log-reset`; commit consumer-group `analytics` offsets after the window update; `default_source()` returns it. Update `supervisor.py` wiring and add readiness = window rebuilt to the topic's end. Keep `HttpScanSource` until T041.

### Tests and wiring for US1

- [ ] T034 [US1] Update `tests/_dispatch.py` `build_apps()` and `tests/conftest.py` to start all four service apps in-process on one shared `MemoryBus` (consumers and reply consumers started in each app's lifespan), with the gateway twin unchanged. Existing mesh twin remains until T047 but is no longer used by flows.
- [ ] T035 [P] [US1] Write `tests/integration/test_catalog_snapshot.py`: Catalog publishes items then marker; Checkout, Inventory and Analytics become ready; an unknown SKU gives `404 SKU_NOT_FOUND`; before the marker a scan gives the clean `503`, and no wrong price is ever recorded.
- [ ] T036 [P] [US1] Write `tests/integration/test_completion_events.py`: complete a multi-item basket and assert the receipt totals equal the scanned prices; an item with too little stock yields `409 INSUFFICIENT_STOCK` and unchanged stock; both outcomes leave exactly one outcome event per request on `inventory.stock.v1`.
- [ ] T037 [P] [US1] Write `tests/integration/test_scan_relay.py`: scans reach `checkout.scans.v1` in `scanId` order; a missing id is skipped only after the grace; a restart of the relay republishes at most the unacknowledged batch and Analytics' counts are unchanged; a `scan-log-reset` clears Analytics' window.
- [ ] T038 [US1] Update existing tests that referenced the HTTP paths, keeping every assertion: `tests/integration/test_checkout_flow.py`, `test_low_stock.py`, `test_popular_items.py`, `test_completion_atomic.py`, `test_concurrent_completion.py`, `test_timeouts.py`, `test_request_id.py`; rename `test_analytics_pull.py` → `test_analytics_events.py` and `test_reserve_idempotency.py` → `test_reserve_events.py`; delete `test_internal_scans.py` (the endpoint is removed in T041, its behavior is covered by T037).
- [ ] T039 [US1] Update `compose.yaml` service definitions: add `KAFKA_BOOTSTRAP: kafka:9092` and `depends_on: kafka-init: condition: service_completed_successfully` to the four services; leave the old peer variables for now. Run `docker compose down -v && docker compose up -d --build --wait`, confirm every unit healthy and `kafka-topics.sh --list` shows the five topics.
- [ ] T040 [US1] Run `uv run pytest tests/contract tests/integration -q` and then `BASE_URL=http://localhost:8080 uv run pytest tests/contract -q` against the stack; fix failures until all pass with no assertion edits (SC-001). Do not proceed to US2 until both are green.

**Checkpoint**: US1 is independently demonstrable: the contract suite passes on the event-driven backend.

---

## Phase 4: User Story 2 - No direct service-to-service calls (Priority: P1)

**Goal**: Delete every service-to-service HTTP path and prove, structurally and by test, that the broker is the only path.

**Independent Test**: [quickstart.md](./quickstart.md) step 3: the boundary check, the isolation check and the no-peer-calls run all pass.

- [ ] T041 [US2] Delete the old HTTP paths: `services/checkout/src/checkout_service/{internal_api.py,inventory_client.py,catalog_client.py}`, `services/inventory/src/inventory_service/{internal_api.py,catalog_client.py}`, `services/analytics/src/analytics_service/catalog_client.py`, `HttpScanSource` and its `_transport` helper in `scan_source.py`, `packages/checkout_web/src/checkout_web/client.py`, `tests/unit/test_client.py`. Remove their routers from each `main.py`. **Keep** `internal_reports.py` in Checkout, Inventory and Analytics (operator check only). Remove `httpx` from `packages/checkout_web/pyproject.toml` and from all four service manifests; re-run T003's locking.
- [ ] T042 [US2] Remove peer addresses: delete `catalog_url`, `inventory_url`, `checkout_url` from the services' `config.py` files and `tests/unit/test_config.py`; remove `CATALOG_URL`, `INVENTORY_URL`, `CHECKOUT_URL` from every service's environment in `compose.yaml`.
- [ ] T043 [US2] Restructure compose networks per [contracts/compose-topology.md](./contracts/compose-topology.md): delete `mesh` and the shared `edge`; add `edge_catalog`, `edge_checkout`, `edge_inventory`, `edge_analytics` (gateway, that service, and the `tools` container) and `bus_catalog`, `bus_checkout`, `bus_inventory`, `bus_analytics` (Kafka and that service); keep `checkout_data` and `inventory_data`. Attach `gateway` to all four `edge_*`, `kafka` to all four `bus_*`, `tools` to all four `edge_*`. Ensure no two services share any network, and `kafka` advertises only `kafka:9092`. Update `gateway/nginx.conf` only if upstream names or networks need it (routes unchanged).
- [ ] T044 [P] [US2] Extend `scripts/check_boundaries.py` with rules E1–E10 from [contracts/service-boundaries.md](./contracts/service-boundaries.md) and update its docstring; replace old rule 5 (outbound HTTP through `checkout_web.client`) and `ALLOWED_PEERS` with E1 and E2; update rules touching networks (7) to E7. Add violating fixtures to `tests/unit/test_boundaries.py` so each rule is proven to fail (a peer URL in compose, `import httpx`, an undeclared topic, two producers for one topic, a consumed topic with no producer, two services on one network, an `/internal/` route other than `/internal/reports/*`, a missing schema, a database import in `checkout_web`).
- [ ] T045 [P] [US2] Rewrite `scripts/check_isolation.sh`: from inside each service container, attempt to resolve and connect to every peer service name and every database it does not own (all must fail), and to `kafka:9092` (must succeed). Exit non-zero on any breach.
- [ ] T046 [P] [US2] Write `scripts/check_no_peer_calls.sh`: with the stack up, run `tests/contract` through the gateway while no network other than the `edge_*`, `bus_*` and data networks exists (already structural after T043), and assert zero connections between service containers by inspecting each container's active connections (`/proc/net/tcp` or `ss` where available) for peer service IPs during the run.
- [ ] T047 [US2] Update `tests/_dispatch.py`: delete `MeshTransport` and its fault hooks (replaced by `MemoryBus` hooks), keep the gateway twin and `ROUTES`; update its docstring to cite spec 008; ensure `tests/unit/test_gateway_routes.py` still equals `gateway/nginx.conf`. Update the shared-code list in [contracts/service-boundaries.md](./contracts/service-boundaries.md) and [plan.md](./plan.md) Project Structure to include `snapshot.py`.
- [ ] T048 [P] [US2] Update `scripts/trace_request.sh`: after the per-service log lines, list the events carrying the request id by reading `checkout.completions.v1` and `inventory.stock.v1` with `kafka-console-consumer.sh --property print.headers=true` and filtering on `x-request-id`; print topic, partition, offset and type.
- [ ] T049 [US2] Run `uv run python scripts/check_boundaries.py`, `uv run pytest tests/unit/test_boundaries.py -q`, `./scripts/check_isolation.sh`, `./scripts/check_no_peer_calls.sh` and `./scripts/trace_request.sh`; all must pass and the trace must show the id in Gateway, Checkout and Inventory logs and on both topics.

**Checkpoint**: US1 and US2 together satisfy the headline requirement. SC-002 and SC-011 are met.

---

## Phase 5: User Story 3 - Completion stays correct and recovers from failure (Priority: P2)

**Goal**: Duplicates, drops, delays, reordering, crashes and outages never produce double-applied stock, negative stock, or a stuck transaction.

**Independent Test**: [quickstart.md](./quickstart.md) step 5: the failure suite, the broker-backed subset and the drills pass, and the invariant holds after each.

- [ ] T050 [US3] Rewrite `tests/failure/_helpers.py` for `MemoryBus` fault hooks (T009): helpers to start the four apps, seed stock, drive a completion, apply a hook at a named step and event type, and assert the final state across both databases.
- [ ] T051 [P] [US3] Rewrite `tests/failure/test_saga_outcomes.py` to cover every row of the outcomes table (A–K) in [completion-flow.md](./contracts/completion-flow.md), each asserting the shopper's response, the transaction's final state, the adjustment state and that no completion is left in-between; include the broker-unavailable case (I) with `fail_publish` and the sweeper delivering later.
- [ ] T052 [P] [US3] Rewrite `tests/failure/test_saga_idempotency.py`: deliver every event twice (`duplicate`) and, in a separate test, ten times; assert the final state equals single delivery (SC-006), including the re-emitted outcome for a repeated request and `REJECTED` replaying the same refusal.
- [ ] T053 [P] [US3] Adapt `tests/failure/test_hot_sku_concurrency.py` to the event path: N concurrent completions for the last units of one SKU end with exactly the available units sold, stock never negative, units sold equal units scanned in completed transactions (FR-013).
- [ ] T054 [P] [US3] Write `tests/failure/test_outbox_crash.py`: simulate a crash after the Checkout step-1 COMMIT and before publish, and after Inventory's COMMIT and before publish; restart; assert the sweeper publishes and the completion reaches a terminal state exactly once (FR-012, FR-014).
- [ ] T055 [P] [US3] Write `tests/failure/test_poison_event.py`: a malformed event and a handler that always raises are dead-lettered after five attempts, later events on the same partition are processed, and a `Transient` outage is not dead-lettered.
- [ ] T056 [P] [US3] Write `tests/failure/test_out_of_order.py`: a release overtaking its reserve (via `reorder`/`delay`) leaves the reserve refused and stock untouched; a scan before the price snapshot is ready fails cleanly and records no price.
- [ ] T057 [US3] Extend the invariant checker for FR-021: add to `services/checkout/src/checkout_service/internal_reports.py` the count of unpublished outbox rows older than 2 s and the maximum published scan id; add the same unpublished-outbox count to `services/inventory/src/inventory_service/internal_reports.py`; add Analytics' highest applied `scanId` to `services/analytics/src/analytics_service/internal_reports.py`. Update `tools/verify_invariant.py` (and its host entry `scripts/verify_invariant.py`) to fail when stock sold ≠ units scanned in completed transactions, any stock is negative, any transaction is `COMPLETING` with no live driver, any outbox row is unpublished, or Analytics' applied id is behind the relay's published id on a quiescent system. Update `tests/integration/test_verify_invariant.py` with a passing and a failing case for each new check.
- [ ] T058 [P] [US3] Write the real-broker subset `tests/broker/` (skipped unless `BROKER_TESTS=1`): `test_topics.py` (topics from `events/topics.yaml` exist with the right partitions and compaction), `test_catalog_compaction.py` (republish keeps one record per SKU), `test_completion_kafka.py` (reserve, reject and release through real Kafka, with keys landing on one partition), `test_dlq_kafka.py`, `test_scan_replay_kafka.py`.
- [ ] T059 [P] [US3] Write `scripts/check_broker_outage.sh`: with load running, `docker compose stop kafka`, wait 20 s, `docker compose start kafka`, wait for `--wait`-style health, let load finish, then run `tools/verify_invariant.py`; also assert no transaction remained `COMPLETING` and the dead-letter topic is empty.
- [ ] T060 [P] [US3] Update `scripts/check_crash_recovery.sh` to `docker compose kill` Checkout and then Inventory (separately) under load, restart each, and run `tools/verify_invariant.py` after each.
- [ ] T061 [US3] Run `uv run pytest tests/failure -q`, `BROKER_TESTS=1 uv run pytest tests/broker -q`, `./scripts/check_broker_outage.sh` and `./scripts/check_crash_recovery.sh`; every run must end with the invariant passing (SC-007).

**Checkpoint**: US3 complete: correctness survives redelivery, loss, reordering, crashes and a broker outage.

---

## Phase 6: User Story 4 - Services run independently of each other's availability (Priority: P2)

**Goal**: Stopping one service never breaks unrelated work; work published while a consumer was down is processed on return; snapshots rebuild from history alone.

**Independent Test**: [quickstart.md](./quickstart.md) step 5 (`check_independence.sh`) plus the three tests below.

- [ ] T062 [P] [US4] Write `tests/integration/test_analytics_replay.py`: stop Analytics, make scans, restart it, assert popular-items reflects every scan made meanwhile (SC-008) and that restart alone (no Checkout call) rebuilds the window from the last `2 × window_size` events.
- [ ] T063 [P] [US4] Write `tests/integration/test_catalog_down.py`: with Catalog stopped, Checkout and Inventory restarted, scanning known SKUs, completing, and low-stock all still work (their snapshots come from the retained topic); `GET /items` returns the contract-shaped `503` within 5 s (SC-009).
- [ ] T064 [P] [US4] Write `tests/integration/test_snapshot_rebuild.py`: a service started with an empty snapshot against an existing topic becomes ready without any call to Catalog; a Catalog restart republishes and consumers' snapshots are unchanged in content.
- [ ] T065 [US4] Update `scripts/check_independence.sh`: for each of Catalog, Checkout, Inventory, Analytics, Kafka stop it, call every public endpoint, and assert that only dependants fail, with a contract-shaped error within 5 s; with Catalog stopped, scan, complete and low-stock must still succeed; with Analytics stopped, everything but popular-items must succeed; restart it and assert catch-up within 30 s.
- [ ] T066 [US4] Run `uv run pytest tests/integration/test_analytics_replay.py tests/integration/test_catalog_down.py tests/integration/test_snapshot_rebuild.py -q` and `./scripts/check_independence.sh`; all pass (SC-008, SC-009).

**Checkpoint**: US4 complete.

---

## Phase 7: User Story 5 - Deliverables: load reports and architecture diagram (Priority: P1)

**Goal**: The repository URL shows both JSON load reports and the architecture diagram.

**Independent Test**: [quickstart.md](./quickstart.md) step 6: open the repo URL and find the two reports and the diagram without running anything.

- [ ] T067 [P] [US5] Write `docs/architecture.md`: the full diagram per [contracts/architecture-diagram.md](./contracts/architecture-diagram.md) as a Mermaid `flowchart` (gateway, four services with private stores, the Kafka broker with all five topics, producer and consumer arrows, outbox and relay inside Checkout and Inventory, and **no service-to-service arrows**), a legend, a table of topics with producers and consumers, and the completion sequence from [completion-flow.md](./contracts/completion-flow.md) as a Mermaid `sequenceDiagram`.
- [ ] T068 [US5] Write `scripts/render_diagram.sh`: render the flowchart block of `docs/architecture.md` to `docs/architecture.svg` with `@mermaid-js/mermaid-cli` (pin an exact version at least two weeks old via `npx`); commit the SVG and reference it from `docs/architecture.md` and the README as a fallback beside the Mermaid block.
- [ ] T069 [P] [US5] Update `scripts/run_load_tests.sh`: default `REPORT_DIR` and all comments and mv paths to `008-event-driven-kafka`; reference spec 008 FR-022; keep the fresh-stack, load, verify-invariant sequence.
- [ ] T070 [US5] Run the **default** load: `REPORT_DIR=008-event-driven-kafka ./scripts/run_load_tests.sh default` (the unmodified `java -cp out Main`) on a fresh stack; confirm zero transport failures and zero server errors, and that `tools/verify_invariant.py` passes afterwards.
- [ ] T071 [US5] Run the **stress** load: `REPORT_DIR=008-event-driven-kafka ./scripts/run_load_tests.sh stress` (`java -cp out Main --stations=100 --duration=120`) on a fresh stack; same checks as T070. Confirm `../load-client/reports/008-event-driven-kafka/` holds **exactly two** `report-*.json` files.
- [ ] T072 [US5] Write `../load-client/reports/008-event-driven-kafka/README.md`: for each report the command, date, throughput, error counts and invariant result; a table beside spec 007's reports (default 200.5 tx/s, stress 196.2 tx/s) with the ratio; if either ratio is below 70 % (SC-010), a measured explanation (use `scripts/step_timings.py` on the logs, expected cause: Kafka round trips on the completion path and per-batch handling in Inventory); a link to `backend/docs/architecture.md`.
- [ ] T073 [US5] Update `README.md` (backend): replace the 007 coordination section with the event flows (catalog, scan, completion, analytics), the topics table with producers and consumers, how to run and test (all of [quickstart.md](./quickstart.md)), the embedded architecture diagram and a link to `docs/architecture.md`, and the 007 comparison (FR-024, SC-005).

**Checkpoint**: US5 complete: both reports and the diagram exist in the repo.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T074 [P] Regenerate locks with `scripts/lock_services.sh` and run `uv run pytest tests/unit/test_service_locks.py -q`; confirm no service lock contains another service's package or `httpx`.
- [ ] T075 [P] Run `uv run python scripts/check_boundaries.py` and the entire `uv run pytest tests -q` once more on a clean checkout of the branch; fix anything that regressed.
- [ ] T076 Walk through [quickstart.md](./quickstart.md) from step 1 to step 7 exactly as written on a fresh machine state (`docker compose down -v` first) and correct any command, path or expectation that is wrong in the quickstart, README or contracts.
- [ ] T077 Update [plan.md](./plan.md) and [research.md](./research.md) with anything learned during implementation (final aiokafka and Kafka versions, measured throughput, any deviation from R1–R15), then commit all work on the feature branch and push it.
- [ ] T078 Confirm the submission: on the pushed branch's GitHub page verify that `load-client/reports/008-event-driven-kafka/` shows two JSON files and that `backend/docs/architecture.md` renders the diagram; record the repository URL in the load-report README.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)** → **Foundational (Phase 2)** → user stories → **Polish (Phase 8)**.
- **US1 (P1)** needs Phase 2 only. It is the MVP.
- **US2 (P1)** needs US1 (it deletes the paths US1 replaced and tests the result).
- **US3 (P2)** needs US1 (the event flows); it can run alongside US2 after T040 except T061, which wants US2's networks (T043) for the drills.
- **US4 (P2)** needs US1; its drill script needs US2's networks.
- **US5 (P1)** needs US1–US4 green: load reports and the diagram describe the finished system. The diagram text (T067) can be drafted earlier.

### Within each story

- Foundational: T005 and T006 before T007; T007 before T008–T011; T008 before T009 and T010; T015 can run any time after T005.
- US1: snapshots (T021–T023) need T020 and T011; completion flow T024 → T025 → T026 → T027 → T028 on Checkout, T029 → T030 → T031 on Inventory (the two sides can proceed in parallel); T032 and T033 are independent of the completion tasks; T034 before T035–T038; T039 → T040 last.
- US2: T041 → T042 → T043; T044–T048 are independent of each other; T049 last.

### Parallel opportunities

- **Phase 2**: T006, T011, T012, T013, T014 together; later T016, T017, T018 together.
- **US1**: T022 and T023 together; Checkout's T024–T028 and Inventory's T029–T031 as two tracks; T035, T036, T037 together.
- **US3**: T051, T052, T053, T054, T055, T056, T058, T059, T060 together once T050 is done.
- **US4**: T062, T063, T064 together.

### Parallel example: User Story 1 tracks

```text
Track A (Checkout): T024 → T025 → T026 → T027 → T028 → T032
Track B (Inventory): T029 → T030 → T031
Track C (Catalog + snapshots): T020 → T021 / T022 / T023
Track D (Analytics): T033
Join: T034 → T035/T036/T037 → T038 → T039 → T040
```

---

## Implementation Strategy

### MVP first (US1 only)

1. Phases 1 and 2, then US1 through T040.
2. **Stop and validate**: the contract suite and the unmodified load client pass against the event-driven backend. The services already exchange events; the old HTTP paths still exist but are unused.

### Incremental delivery

1. Add US2 (delete the HTTP paths, isolate networks, boundary check): the headline requirement is now provable.
2. Add US3 and US4 (correctness and independence evidence).
3. Add US5 (diagram, two load reports, README) last, on the finished system, so the reports measure what is submitted.

### Risks to watch

- **Throughput** (R14): measure early. After T040, run a quick `java -cp out Main --duration=30` and compare with 007's 200.5 tx/s; if far lower, tune `linger_ms`, partition count and Inventory's batch concurrency before US5, not after.
- **Reply consumer readiness** (R5): a worker that reports healthy before its reply consumer is positioned can lose an outcome; T026 gates `/healthz` on it.
- **Schema changes**: the `REJECTED` state and the new tables need a fresh database volume; always `down -v` before compose tests.

---

## Notes

- Total: 78 tasks (Setup 4, Foundational 15, US1 21, US2 9, US3 12, US4 5, US5 7, Polish 5).
- Commit after each task or logical group; each phase's checkpoint is a safe stopping point.
