# Feature Specification: Event-Driven Microservice Communication (Kafka Event Broker)

**Feature Branch**: `008-event-driven-kafka`

**Created**: 2026-10-26

**Status**: Draft

**Input**: User description: "Implement all across-microservice communications using events. To achieve this you'll need to incorporate Kafka. Submit a URL for your repo that contains the 2 json files resulting from running the load client against your server, and an architecture diagram: with default parameters (2 points); from using stress mode, ie java -cp out Main --stations=100 --duration=120 (2 points); architecture diagram showing microservices architecture and event broker (1 point)."

## Overview

Spec 007 split the backend into four microservices (Catalog, Checkout, Inventory, Analytics) behind
an API Gateway, each with a private data store. They still talk to each other **directly**:
Checkout calls Catalog to price a scan, Checkout orchestrates Inventory during completion with
request/response calls and compensations, Analytics polls Checkout's scan log, and Inventory
fetches item names from Catalog. Each of those is a point-to-point dependency: the caller must
know the callee's address, wait for it, and fail when it is down.

This feature replaces **every service-to-service call** with **events published to and consumed
from a Kafka event broker**. A service announces facts about its own data ("a scan was recorded",
"a transaction was completed", "stock was reduced"); interested services react on their own time.
No microservice calls another microservice directly any more. The only synchronous traffic left is
shopper → Gateway → the owning service, which is the public contract and does not change.

The external behavior the load client sees does not change, and the frozen contract (`spec.yaml`)
and unmodified load client still work. What changes is how the services cooperate behind the
Gateway, and the deliverables prove it: two load reports and an architecture diagram.

### Communication that becomes event-driven

| Today (spec 007) | After this feature |
| --- | --- |
| Checkout asks Catalog for an item's price when a scan is recorded | Catalog publishes its fixed item list (SKU, name, price) once at startup and again on each restart; Checkout keeps a local snapshot built from it and prices scans from it |
| Checkout orchestrates Inventory step by step during completion, with direct calls and compensation | Checkout publishes "transaction completed"; Inventory reacts, reduces stock, and publishes the outcome (stock reduced, or rejected for insufficient stock); Checkout reacts to the outcome and finalizes or compensates |
| Analytics polls Checkout's scan log by sequence number | Checkout publishes each scan as an event; Analytics consumes them |
| Inventory and Analytics ask Catalog for item names when answering requests | They use a local snapshot of item names built from Catalog's events |

Services never read each other's stores and never call each other. The broker is the only path
between them.

### What the public contract must still do

Completing a transaction must still answer the shopper with the contract-defined result (receipt
on success, the contract's business error on insufficient stock). Because the stock decision now
happens in another service via events, the shopper-facing request must **wait for the outcome
event** (within a bounded time) rather than answer early.

## Clarifications

### Session 2026-10-26

- Q: Does "all" cross-service communication mean literally no direct calls between microservices? → A: Yes. After this feature, the only HTTP calls are shopper → Gateway → owning service. Health checks and operator tooling are not service-to-service business communication and are out of this rule.
- Q: Is the external contract still frozen? → A: Yes. `spec.yaml` and the unmodified load client are unchanged.
- Q: Which broker? → A: Kafka, as required by the assignment.
- Q: Must the shopper still get a synchronous answer to `complete`? → A: Yes. The contract is request/response; the service waits for the outcome event up to a time limit and then answers or fails cleanly.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Unchanged checkout experience over events (Priority: P1)

A shopper (the load client) starts a transaction, scans items, completes it, and later views
low-stock and popular-items, all through one address, exactly as before. They cannot tell that the
services now cooperate through a broker instead of calling each other.

**Why this priority**: The refactor is worthless if it breaks the frozen contract. Everything else
builds on it.

**Independent Test**: Run the existing contract and integration tests, and the unmodified load
client, against the Gateway. All pass with no assertion changes.

**Acceptance Scenarios**:

1. **Given** the whole system is up, **When** the existing contract tests run against the Gateway, **Then** they all pass unchanged.
2. **Given** a transaction with several scanned items, **When** the shopper completes it, **Then** they receive a receipt whose totals match the prices of the scanned items.
3. **Given** an item with too little stock, **When** the shopper completes a transaction containing it, **Then** they receive the contract's insufficient-stock error and no stock changes.
4. **Given** a completed transaction, **When** low-stock and popular-items are requested, **Then** they reflect that transaction.

---

### User Story 2 - No direct service-to-service calls (Priority: P1)

An operator or reviewer can verify that the microservices communicate only through the event
broker: no service holds another service's address or calls its endpoints.

**Why this priority**: This is the core requirement ("all across microservice communications using
events"). Without it the feature is not delivered even if tests pass.

**Independent Test**: An automated check inspects each service's configuration and code for
outbound calls to other services, and a runtime test blocks service-to-service network paths and
confirms the full workflow still succeeds.

**Acceptance Scenarios**:

1. **Given** the deployed system, **When** network paths between microservices (other than to the broker) are blocked, **Then** every public endpoint still works.
2. **Given** the service configuration, **When** the automated check runs, **Then** it finds zero configured addresses of other microservices and zero outbound calls to other services.
3. **Given** a request that touches several services, **When** an operator inspects the broker, **Then** every cross-service interaction is visible as events on named topics.

---

### User Story 3 - Completion stays correct and recovers from failure (Priority: P2)

Completing a transaction reduces stock exactly once and ends either fully completed or cleanly
rejected, even when events are redelivered, services crash mid-flow, or the broker is briefly
unavailable.

**Why this priority**: Replacing calls with events introduces redelivery and delay; correctness
must survive that.

**Independent Test**: Inject duplicate events, drop and delay events, and stop each service at each
step of completion; run the cross-store correctness check afterwards.

**Acceptance Scenarios**:

1. **Given** a "transaction completed" event is delivered twice, **When** Inventory processes both, **Then** stock is reduced once.
2. **Given** Inventory is stopped when a completion is requested, **When** the time limit passes, **Then** the shopper gets a clean contract-shaped failure, and when Inventory restarts the transaction is either completed or compensated, never stuck.
3. **Given** concurrent completions competing for the last units of an item, **When** all finish, **Then** stock is never negative and units sold equal units scanned in completed transactions.
4. **Given** the broker is unavailable for a short period, **When** it returns, **Then** no event is lost and no transaction stays in an in-between state.

---

### User Story 4 - Services run independently of each other's availability (Priority: P2)

Because services no longer wait on one another, stopping one does not break unrelated work, and
work that arrived while a consumer was down is processed when it returns.

**Why this priority**: This is the benefit that justifies the architecture.

**Independent Test**: Stop Analytics, run scans, restart it, and check popular-items. Stop Catalog
and confirm transaction start, scanning of already-known items, completion and low-stock still work.

**Acceptance Scenarios**:

1. **Given** Analytics is stopped during a load run, **When** it restarts, **Then** popular-items reflects all scans made meanwhile.
2. **Given** Catalog is stopped, **When** shoppers scan items already known to Checkout and complete transactions, **Then** those succeed.
3. **Given** a service starts for the first time or after data loss, **When** it comes up, **Then** it rebuilds its snapshot of other services' data from the event history without calling those services.

---

### User Story 5 - Deliverables: load reports and architecture diagram (Priority: P1)

The submitter provides a repository URL containing the two load-client JSON reports and an
architecture diagram, so a grader can verify the system works and see how it is built.

**Why this priority**: These are the graded deliverables (2 + 2 + 1 points).

**Independent Test**: Open the repository URL; find both reports and the diagram without running
anything.

**Acceptance Scenarios**:

1. **Given** the finished system, **When** the load client runs with default parameters, **Then** its JSON report is committed in a subfolder for this spec.
2. **Given** the finished system, **When** the load client runs in stress mode (`java -cp out Main --stations=100 --duration=120`), **Then** its JSON report is committed beside the first.
3. **Given** the repository, **When** a reader opens the architecture documentation, **Then** a diagram shows every microservice, its private store, the API Gateway, the event broker, and the topics linking producers to consumers.

---

### Edge Cases

- **Duplicate event delivery**: every consumer must produce the same final state whether an event arrives once or several times.
- **Out-of-order events**: events for one transaction or item must be processed in the order produced; consumers must tolerate arrival of a scan before the price snapshot has caught up (retry or clean failure, not wrong price).
- **Price snapshot not yet populated** (cold start): scanning an item whose price is not yet known fails cleanly in the contract's error shape rather than recording a wrong price.
- **No reply within the time limit**: the shopper request fails cleanly and the late outcome is reconciled so no transaction stays in-between.
- **Poison event** (malformed or unprocessable): it is set aside for inspection without blocking later events.
- **Broker unavailable on publish**: no state change is acknowledged to the shopper unless its event is durably recorded for publication; nothing is silently dropped.
- **Consumer crash after processing but before acknowledging**: redelivery has no additional effect.
- **Event schema change**: a new producer version must not break existing consumers during a rolling upgrade.
- **Stock exhausted by concurrent completions**: exactly the available units are sold; the rest get the insufficient-stock error.

## Requirements *(mandatory)*

### Functional Requirements

**Event-only communication**

- **FR-001**: The external HTTP contract in `spec.yaml` MUST remain unchanged, and the unmodified load client MUST work against a single public address.
- **FR-002**: Every business interaction between microservices MUST occur through events on the event broker. No microservice MAY call another microservice's endpoints or hold another microservice's address.
- **FR-003**: The event broker MUST be Kafka. Each microservice MUST be a producer, a consumer, or both, and the system MUST define a named topic for each kind of fact (at minimum: the published catalog, scans recorded, transaction completion requested, stock outcome).
- **FR-004**: Each event MUST carry a unique event identifier, a type, the correlation identifier of the originating request, a timestamp, and a versioned payload described in a written schema.
- **FR-005**: Events MUST be keyed so that all events about the same transaction (and all events about the same item) are processed in order.

**Replacing each existing call**

- **FR-006**: Catalog's items are fixed after seeding, so Catalog MUST publish the full item list (SKU, name, price) as events once at startup and again on every restart, retained so that a late or restarted consumer can read the whole list. Checkout MUST price scans from a local snapshot built from those events, and MUST NOT call Catalog.
- **FR-007**: Checkout MUST publish each recorded scan as an event. Analytics MUST consume those events to build popular-items, and MUST NOT poll or call Checkout.
- **FR-008**: Completing a transaction MUST be coordinated through events: Checkout publishes a completion request; Inventory reduces stock and publishes an outcome (success or insufficient stock); Checkout reacts to the outcome by finalizing or compensating. The shopper-facing request MUST wait for the outcome up to a bounded time and then return the contract result or a clean failure.
- **FR-009**: Inventory and Analytics MUST obtain item names from a local snapshot built from Catalog's events, not from calls.
- **FR-010**: A snapshot of another service's data MUST have a documented owner and source topic, and MUST be rebuildable from the event history alone.

**Correctness**

- **FR-011**: Every consumer MUST be idempotent: redelivering any event MUST NOT apply its effect twice.
- **FR-012**: A state change and the publication of its event MUST NOT be able to diverge: either both happen or neither, including across a crash between them.
- **FR-013**: Stock MUST never go negative, and total units sold MUST equal total units scanned in completed transactions, under concurrent completions.
- **FR-014**: A completion interrupted by a crash, lost reply, or broker outage MUST be resumed or compensated after recovery. No transaction MAY remain indefinitely in an in-between state.
- **FR-015**: An event that repeatedly fails processing MUST be isolated for inspection and MUST NOT block later events.

**Resilience, observability, operations**

- **FR-016**: Stopping one microservice MUST NOT break endpoints that do not depend on it. Work published while a consumer was down MUST be processed when it returns, with no loss.
- **FR-017**: The correlation identifier of a request MUST appear on every event it causes and in the logs of every service that handles it, so a request can be traced across services.
- **FR-018**: The broker MUST be part of the runnable system (started, health-checked and ordered before the services that use it), and each service MUST declare the topics it produces and consumes.
- **FR-019**: An automated check MUST fail on any service-to-service call, any configured address of another microservice, and any consumer of a topic with no declared producer.
- **FR-020**: Automated tests MUST cover duplicate delivery, out-of-order arrival, consumer crash mid-flow, broker outage, and a stopped service at each step of completion.
- **FR-021**: A correctness check across all stores MUST verify the invariants (stock sold equals units scanned in completed transactions, no negative stock, no stuck transactions, Analytics has consumed every scan) on a quiescent system.

**Deliverables**

- **FR-022**: Two load-client JSON reports MUST be produced against this system and committed in a subfolder named for this spec: one with default parameters, one from stress mode (`java -cp out Main --stations=100 --duration=120`).
- **FR-023**: An architecture diagram MUST be committed and referenced from the README. It MUST show the API Gateway, all four microservices with their private stores, the Kafka event broker, and the topics with the direction of each producer and consumer.
- **FR-024**: The README MUST describe the event flows (scan, completion, analytics feed, catalog snapshot), list topics with their producers and consumers, explain how to run and test, and compare results with spec 007 and explain any difference.

### Key Entities

- **Event**: An immutable record of a fact, with identifier, type, correlation identifier, timestamp, key, and versioned payload.
- **Topic**: A named, ordered, durable stream of one kind of event, with one owning producer service.
- **Item snapshot**: A service-local copy of Catalog's item names and prices, built from the catalog Catalog publishes at startup; owned by Catalog, read-only elsewhere.
- **Transaction**: A shopper's basket owned by Checkout; moves from open to completion-requested to completed or rejected.
- **Completion request / stock outcome**: The pair of events through which Checkout and Inventory agree on whether a transaction's stock can be taken.
- **Scan event**: A record that an item was scanned into a transaction, consumed by Analytics.
- **Processed-event record**: A consumer's memory of events it already applied, enabling idempotency.
- **Load report**: The load client's JSON output for one run.
- **Architecture diagram**: A picture of gateway, microservices, stores, broker and topics.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the existing contract and integration tests pass against the Gateway with no changes to test assertions.
- **SC-002**: With all service-to-service network paths blocked except to the broker, 100% of public endpoints still work, and the automated check reports zero direct inter-service calls.
- **SC-003**: The default load run and the stress run (100 stations, 120 seconds) both complete with zero transport failures and zero server errors (only contract-defined business errors such as insufficient stock), and the cross-store correctness check passes after each.
- **SC-004**: Two load reports (default, stress) are present in the repository under this spec's subfolder and openable from the submitted URL.
- **SC-005**: The architecture diagram lets a reader name every microservice, its store, the broker, and which services produce and consume each topic, without reading code.
- **SC-006**: Delivering every event 2 times (and, in a separate run, 10 times) produces the same final state as delivering it once.
- **SC-007**: In failure-injection runs that stop each service at each completion step, 100% of runs end with all stores consistent (fully completed or fully undone) and no negative stock.
- **SC-008**: With Analytics stopped during a load run and restarted, popular-items reflects all scans made meanwhile within 30 seconds of its recovery.
- **SC-009**: Stopping any one microservice leaves 100% success on every endpoint that does not depend on it, and dependent endpoints answer cleanly within 5 seconds.
- **SC-010**: Stress-run throughput (completed transactions per second) is reported next to spec 007's. If below 70% of that figure, the README explains the measured cause.
- **SC-011**: Any single request can be traced across all services and events it caused using its one correlation identifier.

## Assumptions

- Spec 007's four microservices, their private stores, and the API Gateway are the starting point; this feature changes how they communicate, not their business responsibilities or public endpoints.
- A single-broker Kafka deployment, run alongside the services in the existing container setup, is sufficient for the assignment; broker clustering and replication across machines are out of scope.
- Health checks and operator tooling (seeding, check scripts) are not business communication and may remain direct.
- "Event-driven" for `complete` still returns a synchronous answer to the shopper (the contract requires it) by waiting for the outcome event within a bounded time.
- Event schemas are written down and versioned in the repository; a schema registry service is not required.
- The architecture diagram is a committed image or diagram-as-text file rendered in the README; no particular drawing tool is required.
- The load reports are produced on the same machine as the spec 007 reports so the comparison is fair.
- Requires the existing load client, `spec.yaml`, the 005/007 test suites, and the container runtime already used by spec 007.
