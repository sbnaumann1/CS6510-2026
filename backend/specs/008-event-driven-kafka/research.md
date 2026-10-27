# Research: Event-Driven Microservice Communication

Decisions for [plan.md](./plan.md). No `NEEDS CLARIFICATION` remained after the spec; these record
the design choices, why, and what was rejected. Spec 007 research (R1–R10) still stands except
where noted.

## R1. Broker and client

**Decision**: Apache Kafka 3.9 in KRaft mode (one container, combined broker+controller), image
`apache/kafka` (multi-arch, runs natively on Apple silicon). Python client **aiokafka**.

**Rationale**: Kafka is mandated. KRaft removes ZooKeeper, so the broker is one container with one
health check. aiokafka is asyncio-native, which matches FastAPI/uvicorn and the existing async
code; the blocking `confluent-kafka`/`kafka-python` clients would need thread executors on the hot
path.

**Alternatives**: `confluent-kafka` (faster, C-based, but callback/thread bridging to asyncio and a
native wheel per platform); Redpanda (Kafka-compatible, but the assignment says Kafka); a
three-broker cluster (out of scope per the spec's assumptions, and the stress run would measure
mostly broker contention on one laptop).

**Settings**: `acks=all` with idempotent producer (on one broker it costs the same as `acks=1` and
protects against producer retry duplicates); `linger_ms=2` so bursts share a batch; consumers use
`enable_auto_commit=false`, committing after the handler's local transaction commits.
`KAFKA_HEAP_OPTS=-Xmx512m` to bound memory beside two PostgreSQL instances.

## R2. Topics, keys, partitions

**Decision**: five topics plus one dead-letter topic ([contracts/events.md](./contracts/events.md)):

| Topic | Key | Partitions | Config |
| --- | --- | --- | --- |
| `catalog.items.v1` | SKU | 1 | compacted |
| `checkout.scans.v1` | none (log order) | 1 | delete, 7 d |
| `checkout.completions.v1` | transaction id | 6 | delete, 1 d |
| `inventory.stock.v1` | transaction id | 6 | delete, 1 d |
| `dead-letters.v1` | source topic | 1 | delete, 14 d |

**Rationale**: Keying completion and outcome events by transaction id puts every event about one
transaction on one partition, so Inventory sees `reserve` before `release` and Checkout sees
outcomes in order (FR-005). Six partitions let Inventory's two workers and Checkout's six workers
process different transactions in parallel. Scans use **one partition** because Analytics' window
is defined by scan order; a single partition fed by a single relay gives that order for free, and
~2 k scans/s is trivial for one partition. The catalog topic is compacted so the latest record per
SKU is kept forever and a fresh consumer reads at most one record per item.

**Alternatives**: partition scans by transaction (needs a merge-by-scan-id in Analytics, which
brings the 007 gap logic into the consumer); one topic for everything (couples unrelated retention
and consumers); partition count per hot SKU (the hot-SKU contention is inside Inventory's database,
not in Kafka).

## R3. Catalog is a one-time publication, not a change stream

**Decision**: Catalog publishes every item (key = SKU, value = name and price) and then a
`catalog-published` marker carrying the item count, **at each startup**. It never publishes again
until restarted. Consumers read `catalog.items.v1` from the beginning into an in-memory snapshot
and become *ready* when they have seen a marker whose count matches the items read.

**Rationale**: 007 established that the catalog is fixed after seeding (a read-only file). There is
nothing to stream, only reference data to hand over once. Republishing at each start covers a
consumer that started first, a consumer whose snapshot was lost, and a broker whose topic was
wiped, with no coordination: duplicates are harmless because a record per SKU overwrites itself.
The marker lets a consumer distinguish "the catalog is empty or not loaded yet" from "this SKU does
not exist", so an unknown SKU is still a clean `SKU_NOT_FOUND` while a cold snapshot is a clean
`503` (spec edge case).

**Alternatives**: Checkout/Inventory/Analytics call `GET /items` (forbidden by FR-002); embed the
catalog file in each image (breaks Catalog's ownership); publish on demand via request/reply (a
call in disguise).

**Effect on availability**: with the topic retained, Checkout can restart while Catalog is down and
still price scans, which 007 could not do after a restart (User Story 4, scenario 2).

## R4. Completion: keep the orchestrated saga, carry it on events

**Decision**: keep 007's saga (states `RESERVING → RESERVED → COMPLETED`, `COMPENSATING`, epochs,
lease, recovery loop). Replace only its transport:

- *reserve* = publish `stock-reservation-requested`, await `stock-reserved` | `stock-rejected` |
  `reservation-refused` for the same `(transaction, epoch)`;
- *release* = publish `stock-release-requested`, await `stock-released`.

Details in [contracts/completion-flow.md](./contracts/completion-flow.md).

**Rationale**: the spec says Checkout "reacts to the outcome by finalizing or compensating", and
007's tests, state machine and recovery already encode the hard cases (crash in each state, late
reserve after release, concurrent complete). Keeping them means the new work is the transport, and
the existing failure tests carry over almost unchanged. It remains orchestration, now with an
event-carried request and reply. A choreographed saga was considered and rejected below.

**Alternatives**: *Choreography* (Checkout emits `transaction-completed`, Inventory emits
`stock-reduced`, Checkout finalizes on its own, with nothing waiting): a stock shortage would have
to un-complete an already-announced completion, so the shopper could be told "complete" and then
see it undone, which breaks the contract (a receipt means stock was taken). The orchestrated form
keeps the contract's answer truthful. *Event sourcing the transaction* (rebuild state from events):
a rewrite of Checkout's store, not asked for.

## R5. How `complete` waits for an event (synchronous answer over events)

**Decision**: each Checkout worker process runs one **reply consumer** with no consumer group
(manual partition assignment to all partitions of `inventory.stock.v1`, starting at the end). The
request handler registers an in-process waiter keyed by `(transaction, epoch)` *before* publishing
the request, publishes, and awaits the waiter with a deadline. The reply consumer resolves the
waiter when the outcome arrives. If no outcome arrives, the request is re-published up to twice
(Inventory's idempotency absorbs repeats), then the saga rolls back exactly as in 007 case B.

**Rationale**: with six Checkout workers, a normal consumer group would deliver an outcome to an
arbitrary worker, not the one holding the shopper's connection. Reading all partitions in every
worker avoids a reply-routing scheme, and each worker discards outcomes it has no waiter for
(~200/s of small messages, negligible). Manual assignment also avoids group-join delays on start.
An outcome missed because the worker was down is not a problem: recovery re-requests, and Inventory
re-emits the recorded outcome (R6).

**Alternatives**: poll `completion.state` in the database (adds database load per waiting request);
`LISTEN/NOTIFY` (an additional mechanism, rejected for Analytics in 005 for related reasons); a
per-worker reply topic (more topics for the same effect).

**Readiness**: a worker reports healthy only after its reply consumer is assigned and positioned,
otherwise an outcome could be published before anyone is listening.

## R6. Inventory side: idempotent handlers, outcome recorded with the stock change

**Decision**: Inventory consumes `checkout.completions.v1` in the durable group `inventory`, commits
the offset **after** its local transaction commits. Reserve and release reuse 007's
`reserve_once` / `release` statements. Each handler writes its outcome event to Inventory's
`outbox` **in the same local transaction**, so a stock change can never exist without its outcome.
A rejected reservation is now recorded as an adjustment in state `REJECTED` (it changes no stock),
so a redelivered request replays the *same* answer instead of re-deciding against new stock. A
repeated request (redelivery or Checkout's re-publish) finds its recorded outcome and **re-emits**
it.

**Rationale**: this is 007's idempotency rule (one record per `(transaction, epoch)`) extended to
cover rejection, and it makes "re-ask, get the same answer" the universal recovery step. The hot-SKU
rule is unchanged: row locks are held for one statement plus COMMIT; Kafka I/O happens after.

**Throughput**: a poll returns a batch; handlers run concurrently across distinct transaction keys
and sequentially within a key, bounded by the pool size, then the batch's offsets are committed. Per
partition order is preserved where it matters (reserve before release).

## R7. Transactional outbox with fast-path publish

**Decision**: Checkout (completion requests) and Inventory (outcomes) write an `outbox` row in the
same local transaction as the state change. After COMMIT the handler publishes the event itself
and marks the row published (**fast path**: no added latency). A **sweeper** every second publishes
rows still unpublished after 2 s (crash between commit and publish, broker outage) and then marks
them. Delivery is at-least-once; every consumer is idempotent.

**Rationale**: FR-012 forbids divergence across a crash. Polling alone adds up to a poll interval to
every completion; fast path alone loses events on a crash. Both together give low latency and no
loss. The sweeper is safe in every worker because publishing twice is harmless and marking is
guarded by the row id.

**Alternatives**: change-data-capture (Debezium) adds a Kafka Connect cluster; Kafka transactions
cannot span the database; publishing before commit announces effects that may roll back.

**Outbox retention**: published rows are deleted by the sweeper after one hour.

## R8. Scans: the scan table is the outbox; one leader relays it

**Decision**: a Checkout **relay** publishes rows of the existing scan table to `checkout.scans.v1`
in scan-id order, keeping its position in a one-row `relay_state` table. One worker at a time runs
it, chosen by a PostgreSQL advisory lock in Checkout's own database (try-lock; the others retry).
Ordering is made safe with 007's gap rule moved here: publish only the contiguous prefix, and
declare a missing id a permanent gap only after a grace period (`POPULAR_GAP_GRACE_MS`, 1 s).
The position is saved after Kafka acknowledges, so a crash re-publishes a few scans.

**Analytics**: `KafkaScanSource` (the existing `ScanSource` seam) reads the topic in order and
ignores any scan id it has already applied (duplicates from relay re-publish). On start it seeks to
`end − window_size × 2` on the single partition and reads forward, which rebuilds the 1 000-scan
window without any call to Checkout. Because `scanId` is gap-free after the relay, the pipeline's
gap logic is no longer needed in Analytics but stays as a harmless guard.

**Rationale**: the scan insert is the hot path (spec 006), so adding an outbox insert per scan would
cost the very thing that was tuned. The scan table already is the durable, ordered record of "a
scan happened", which is exactly what an outbox row is. Publishing straight from the handler would
lose scans on a crash.

**Alternatives**: a second table written in the scan CTE (extra write per scan); Kafka producer in
the scan handler with `send_and_wait` (adds broker latency to every scan and still loses on crash
between COMMIT and send); partition scans by transaction (R2).

## R9. Consumer failure handling: retry, then dead-letter

**Decision**: a handler that raises is retried with backoff (5 attempts, 0.2 s doubling). After the
last attempt the event, its error and its origin are published to `dead-letters.v1` and the offset
is committed so later events proceed. Transient infrastructure errors (database or broker
unreachable) are **not** counted toward dead-lettering: the consumer pauses and retries until the
dependency returns.

**Rationale**: FR-015. A poison event must not block the partition; an outage must not discard good
events.

## R10. Event format and versioning

**Decision**: JSON envelope `{eventId, type, version, correlationId, occurredAt, producer, data}`;
Kafka key carries the ordering key; Kafka header `x-request-id` mirrors `correlationId` for log
tracing without parsing the body. One JSON Schema per event type in `events/schemas/`, validated in
tests (not on the hot path). Compatibility rule: add optional fields only; a breaking change gets a
new `type` or `version`, and consumers ignore unknown fields and reject (dead-letter) unknown
major versions.

**Rationale**: matches FR-004; JSON keeps debugging with plain tools possible; a schema registry is
out of scope per the spec's assumptions.

**Alternatives**: Avro/Protobuf with a registry (another service to run and learn for a one-machine
assignment).

## R11. Correlation identifier across events

**Decision**: the request id from the gateway (007) is put in `correlationId` and the
`x-request-id` header of every event a request causes. Consumers set it as the request id for the
handler, so existing `log_step` lines carry it unchanged. Events caused by the sweeper or recovery
use a fresh id and, where possible, the original transaction id as a second field in the log line.

**Effect**: `scripts/trace_request.sh` works as in 007 and additionally lists the events (topic,
type, offset) carrying that id via `kafka-console-consumer`.

## R12. Network isolation by construction

**Decision**: remove `mesh`. Each service gets an `edge_<svc>` network shared with the gateway (and
with the operator tools container, which calls `/internal/reports/*`), and a `bus_<svc>` network
shared with Kafka. Each database keeps its private network. No two services share any network.
Kafka advertises one listener, `kafka:9092`, resolvable on every `bus_*` network.

**Rationale**: FR-002 becomes a property of the topology rather than of discipline: a service that
tried to call a peer would fail to resolve its name. The runtime drill (SC-002) therefore needs no
firewall tooling.

**Alternatives**: one shared `bus` network (services could then reach each other directly on it);
iptables rules in containers (fragile, needs privileges).

## R13. Testing without a broker, and with one

**Decision**: services depend on the `Bus` interface. Unit, contract, integration and failure tests
run on `MemoryBus`, an in-process bus with the same per-key ordering and offsets, plus hooks to
drop, duplicate, delay, reorder and "crash before commit" chosen events. A `tests/broker/` subset
runs the same flows against a real Kafka started by `compose.test.yaml`, and the compose drills use
the full stack.

**Rationale**: 007's failure tests are deterministic because the mesh is in-process; `MemoryBus`
keeps that property (SC-006, SC-007). Real-broker tests prove the Kafka-specific parts (compaction,
keys, offsets, rebalances, DLQ) that a fake cannot.

## R14. Performance expectations

**Observation from 007**: default 200.5 tx/s, stress 196.2 tx/s; the hot path is the scan CTE and
the single-statement reserve.

**Expectation**: scans cost nothing extra on the request path. Completion adds one produce, one
consume and one produce/consume round trip (≈ 10–40 ms on a local broker) instead of one HTTP
call, so completion latency rises while throughput is bounded by the closed-loop load client's pacing
and by Inventory's per-batch handling. The plan's mitigations are batching, concurrent handlers per
batch and `linger_ms=2`. Results are reported beside 007's (SC-010), with a measured explanation if
below 70 %.

## R15. Deliverables and the diagram

**Decision**: reports are produced by the existing `run_load_tests.sh` with
`REPORT_DIR=008-event-driven-kafka` and committed under `load-client/reports/008-event-driven-kafka/`.
The architecture diagram is Mermaid in `backend/docs/architecture.md` (GitHub renders it from the
repo URL) with a rendered SVG committed beside it, linked from the README
([contracts/architecture-diagram.md](./contracts/architecture-diagram.md)).

**Rationale**: the grader opens one URL; a Mermaid block renders in place and the SVG is a fallback
if rendering is unavailable.
