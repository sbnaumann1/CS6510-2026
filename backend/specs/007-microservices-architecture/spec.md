# Feature Specification: Microservices Architecture (Database per Service, Coordinated Cross-Service Updates)

**Feature Branch**: `007-microservices-architecture`

**Created**: 2026-10-19

**Status**: Draft

**Input**: User description: "Redesign the self-checkout backend solution to be based on independently deployable microservices. Each microservice - probably based on the services identified in the previous assignment - should have its own data store. Requirements to update multiple databases for an API request should be coordinated across microservices. You can use an API Gateway and orchestration or choreography."

## Overview

Spec 005 split the backend into four domain services that all read and write **one shared database**
and never call each other. Their only coupling was the shared data-access library and the tables
themselves. That made a schema change a multi-service redeploy and let any service read another's
data directly.

This feature finishes the job. Each service becomes a **microservice**: independently deployable,
owning a **private data store** that no other service can touch. When one customer-facing request
needs data owned by several services, the services **cooperate through their own public
interfaces** to get the work done, and the system coordinates multi-store updates so they either
all take effect or are all undone. The external behavior the load client sees does not change.

### Microservices defined by this feature

| Microservice | Business function | Public endpoints it owns | Data it owns (private store) |
| --- | --- | --- | --- |
| **Catalog** | What can be sold, and at what price | `GET /items` | Item catalog (SKU, name, price), held in a private read-only file |
| **Checkout** | Running a customer's basket from start to payment | `POST /transactions`, `POST /transactions/{id}/items`, `POST /transactions/{id}/complete`, `GET /transactions/{id}` | Transactions and their scanned items; the record of each multi-service completion in progress |
| **Inventory** | Knowing how much of each item is on hand, and what needs restocking | `GET /inventory/low-stock` | Stock levels and low-stock alerts |
| **Analytics** | Understanding customer buying behavior | `GET /analytics/popular-items` | Nothing persisted: a rolling in-memory window of the most recent scans and the current ranking, both derived from Checkout's scans |

An **API Gateway** sits in front of all four on the single public address the load client targets.
It routes each request to the owning microservice and holds no business data. It is infrastructure,
not a domain service.

Which request touches which stores:

| Request | Stores involved |
| --- | --- |
| `GET /items` | Catalog |
| `POST /transactions` | Checkout |
| `POST /transactions/{id}/items` | Checkout (needs the item's price from Catalog); Analytics reads the scan from Checkout asynchronously |
| `POST /transactions/{id}/complete` | Checkout (finalize), Inventory (reduce stock, raise low-stock alerts) |
| `GET /inventory/low-stock` | Inventory (item names from Catalog) |
| `GET /analytics/popular-items` | Analytics (item names from Catalog) |

Completing a transaction is the one request that must update more than one store before it
answers. It is the coordination problem this feature must solve. Analytics is fed separately:
popular-items has always counted every scan, whether or not its transaction is completed, so
Analytics **reads** new scans from Checkout continuously, asynchronously and without blocking the
shopper. Checkout does not know Analytics exists. Analytics keeps only what it can recompute from
Checkout's scans, so it needs no database of its own.

## Clarifications

### Session 2026-10-19

- Q: Which coordination style? → A: Left open by the assignment. This spec requires the *outcomes* (all-or-nothing visible results, no lost or double-applied updates). The plan will choose orchestration or choreography and justify it.
- Q: Is the external contract still frozen? → A: Yes. `spec.yaml` and the unmodified load client are unchanged.
- Q: Does one shared database server with separate private schemas count as "its own data store"? → A: No. Each microservice gets its own separately running data store instance that is not reachable by any other microservice.
- Q: Does every store have to be a database server? → A: No. Catalog's data is a fixed reference list that never changes after seeding, so its private store is a read-only file that only Catalog (and its seed step) can open. Checkout and Inventory change data under load and keep database instances.
- Q: Does Analytics need a store at all? → A: No. Popular-items is derived from Checkout's scans, which are already durable there. Analytics keeps a rolling window in memory and rebuilds it from Checkout after a restart. Only one Analytics instance runs, so no leader election is needed.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Unchanged checkout experience on microservices (Priority: P1)

A shopper (the load client) starts a transaction, scans items, completes it, and later views
low-stock and popular-items information, all through one address, exactly as before. They cannot
tell the backend is now several microservices with separate data stores.

**Why this priority**: The refactor is worthless if it breaks the frozen contract. Everything else
builds on this.

**Independent Test**: Run the existing contract and integration tests, and the unmodified load
client, against the Gateway. All pass with no assertion changes.

**Acceptance Scenarios**:

1. **Given** the whole system is up, **When** the existing contract tests run against the Gateway, **Then** they all pass unchanged.
2. **Given** a transaction with several scanned items, **When** the shopper completes it, **Then** they receive a receipt whose totals match the prices of the scanned items.
3. **Given** a completed transaction, **When** low-stock and popular-items are requested, **Then** the results reflect that sale.
4. **Given** the default load-client run, **When** it finishes, **Then** it reports no transport failures and the correctness invariant (stock sold equals scans) holds.

---

### User Story 2 - Each microservice owns its data and is its own deployable unit (Priority: P1)

An operator can build, start, stop, and redeploy any one microservice on its own. Each one has its
own private data store. No microservice can read or write another's data except by calling that
microservice's public interface.

**Why this priority**: This is the defining property of the architecture and what the assignment
grades.

**Independent Test**: Inspect the deployment topology and run the boundary check. Stop one
microservice and observe the rest.

**Acceptance Scenarios**:

1. **Given** the deployment definition, **When** an operator lists the running parts, **Then** each of the four microservices has its own data store, and no data store is shared by two microservices.
2. **Given** a microservice's credentials and network position, **When** it tries to reach another microservice's data store, **Then** it cannot.
3. **Given** the boundary check, **When** it scans the code, **Then** it reports zero cross-service imports of data-access code and zero direct data-store connections to a store the service does not own.
4. **Given** one microservice is rebuilt and redeployed, **When** the others are left running, **Then** they need no rebuild or restart.
5. **Given** Catalog is stopped, **When** shoppers use the Inventory, Analytics and transaction-start endpoints, **Then** those still work. (Endpoints that need Catalog data fail cleanly; see Edge Cases.)

---

### User Story 3 - Multi-store completion is coordinated and all-or-nothing (Priority: P1)

When a shopper completes a transaction, Checkout and Inventory must both record their part.
If either part cannot be done, the system must not leave the stores disagreeing: the shopper sees either
a successful completion with both stores updated, or a clear failure with every earlier step undone.

**Why this priority**: This is the hard distributed-systems requirement and the reason the stores
can't just be updated independently.

**Independent Test**: Complete transactions normally, then repeat with Inventory and Checkout
each forced to fail or time out at each step. After each run, check that the stores agree.

**Acceptance Scenarios**:

1. **Given** a transaction with scanned items and enough stock, **When** it is completed, **Then** Checkout shows it completed and Inventory shows stock reduced by exactly the scanned units.
2. **Given** a basket that needs more stock than Inventory holds, **When** completion is attempted, **Then** the shopper gets the contract's insufficient-stock conflict, no stock changes, and the transaction is not marked completed.
3. **Given** Inventory has reduced stock but a later step fails, **When** the failure is detected, **Then** the stock reduction is reversed and the transaction is not left half-completed.
4. **Given** Analytics is down while shoppers scan and complete, **When** it recovers, **Then** it reads Checkout's scans, counts every scan made meanwhile exactly once, and popular-items reflects them.
5. **Given** a completion request is retried or runs twice at once (client timeout, duplicate delivery), **When** it is processed, **Then** the stock is reduced once, not twice. A second completion while the first is still running, or after it has finished, gets the contract's existing "not open" conflict. A completion whose driver has died is taken over by a retry once its lease has expired.
6. **Given** the system is stopped in the middle of a completion and restarted, **When** it comes back, **Then** the interrupted completion is finished or rolled back, never left in limbo.

---

### User Story 4 - Failures stay contained and observable (Priority: P2)

When a microservice is slow or down, only the requests that depend on it fail, they fail quickly with
the contract's error shape, and an operator can see which step of which request failed.

**Why this priority**: Independent deployability is only useful if failure is isolated, and a
distributed flow must be debuggable.

**Independent Test**: Stop each microservice in turn and exercise every endpoint. Confirm
which succeed and which return a clean error, and find the failed step in the logs.

**Acceptance Scenarios**:

1. **Given** Inventory is down, **When** a shopper completes a transaction, **Then** they get a clean, contract-shaped error within a bounded time, and no store is left partially updated.
2. **Given** any cross-service request, **When** it runs, **Then** a single request identifier appears in every microservice's log for that request.
3. **Given** a slow dependency, **When** a call to it exceeds its time limit, **Then** the caller stops waiting and applies the failure handling instead of hanging.

---

### User Story 5 - Graded load reports for the microservices architecture (Priority: P2)

The submission lists the microservices defined, explains the coordination approach, and includes two
load-client JSON reports: default parameters, and stress mode (`--stations=100 --duration=120`).

**Why this priority**: These are the deliverables.

**Independent Test**: Run both configurations from a clean start and check the reports are produced
and the invariant passes.

**Acceptance Scenarios**:

1. **Given** a clean start, **When** the default load run finishes, **Then** a JSON report is saved and the correctness invariant passes.
2. **Given** a clean start, **When** the stress run finishes, **Then** a JSON report is saved and the invariant passes.
3. **Given** the README, **When** a reader reads it, **Then** they can name each microservice, its data store, how multi-store updates are coordinated, and the throughput change from spec 005.

---

### Edge Cases

- **A microservice is down**: the Gateway returns a gateway-unavailable error in the contract's error shape for that microservice's routes only. Requests that need it as a dependency fail cleanly or complete later, as described below.
- **Catalog is down during a scan**: Checkout prices from a cached snapshot of the catalog, so scans keep working once it has been loaded. If Checkout has never loaded the catalog, the scan fails with a clean error and nothing is recorded. Completion never needs Catalog, because Checkout stores the price it saw at scan time.
- **Inventory is down during completion**: completion fails cleanly. The transaction stays open so the shopper can retry.
- **Analytics is down**: scanning and completing continue unaffected. When Analytics returns it rebuilds its window from Checkout's scans and counts each scan once. Popular-items is allowed to be briefly stale.
- **Analytics restarts while Checkout is down**: Analytics has no store of its own, so it cannot rebuild until Checkout returns. Popular-items answers with the empty window in the meantime. This is accepted: the data is derived and recomputed within seconds of Checkout's return.
- **Checkout is down while Analytics runs**: Analytics keeps serving its last ranking from memory and resumes reading when Checkout returns.
- **Two shoppers complete baskets competing for the last units**: exactly the units available are sold. The loser gets the insufficient-stock conflict. Stock never goes negative.
- **Duplicate or retried completion**: no double stock reduction. A concurrent duplicate, and a repeat after completion, get the contract's "not open" conflict. A retry after a refused completion (for example while Inventory was down) works as soon as Inventory is back.
- **Analytics reads the same scan twice** (for example after a retry): counted once.
- **A coordination step succeeds but its acknowledgement is lost**: retrying the step has no additional effect.
- **A microservice crashes mid-completion**: after restart, the in-progress completion is resumed or compensated. No transaction stays permanently "completing".
- **Abandoned transactions**: the sweep that cancels stale transactions stays within Checkout's own data. It never leaves stock reserved, because stock is only reduced at completion.
- **Stores briefly disagree**: during a completion in flight, stores may differ. Once the request returns (or recovery finishes), they agree. The invariant check is defined for a quiescent system.
- **Catalog data changes**: names and prices are fixed for the duration of a run. Re-seeding means bringing the whole stack down and up again.
- **Data store not ready at start**: each microservice waits for its own store, and seeding finishes before it serves traffic.
- **Docker daemon not running**: fails fast with Docker's own error. The README lists starting Docker as a prerequisite.

## Requirements *(mandatory)*

### Functional Requirements

**Contract and behavior**

- **FR-001**: The external HTTP contract in `spec.yaml` MUST remain unchanged, and the unmodified load client MUST work against a single public address.
- **FR-002**: The system MUST consist of four microservices (Catalog, Checkout, Inventory, Analytics) behind an API Gateway, each owning the endpoints listed in the Overview.
- **FR-003**: The Gateway MUST route each public request by path to the owning microservice, MUST contain no business logic or business data, and MUST return a contract-shaped error when the target is unavailable.

**Independent deployment**

- **FR-004**: Each microservice MUST be buildable, startable, stoppable and redeployable independently of the others, with its own image, dependency set, configuration and health check.
- **FR-005**: Microservices MUST NOT share a code library that contains data-access logic or data schemas. Any shared code MUST be limited to cross-cutting, data-free concerns (for example the contract error shape and health endpoint), and the plan MUST list it explicitly.
- **FR-006**: A change to one microservice's data schema MUST NOT require rebuilding or redeploying any other microservice.

**Database per service**

- **FR-007**: Each microservice MUST own a separate private data store, and no store MUST be shared by two microservices. A store MUST be a separately running database instance where the data changes at runtime and cannot be recomputed (Checkout, Inventory). Catalog's fixed reference data MAY be a read-only file private to Catalog. Analytics holds only derived data that it can rebuild from Checkout's scans, so it MAY keep that data in memory with no persistent store.
- **FR-008**: A microservice MUST be able to access only its own data store. Other microservices' stores MUST be unreachable from it by network position, credentials or file access, not merely by convention.
- **FR-009**: A microservice that needs another's data MUST obtain it through that microservice's public interface, never by reading its store. Any copy it keeps (for example, a price captured at scan time) MUST be treated as a snapshot, with its owner and source documented.
- **FR-010**: Seeding MUST populate each store with only the data its microservice owns (catalog items for Catalog, initial stock for Inventory), as an operator step before the microservices serve traffic.

**Coordinated multi-store updates**

- **FR-011**: Completing a transaction MUST update Checkout and Inventory as one coordinated operation, such that the shopper sees either full success or a clean failure with all earlier effects undone.
- **FR-012**: The coordination MUST NOT rely on a single cross-database transaction. It MUST use a distributed-workflow approach (orchestrated or choreographed saga, or equivalent) with explicit compensation for steps that can be undone.
- **FR-013**: Every coordination step MUST be idempotent. Retrying or redelivering a step MUST NOT apply its effect twice.
- **FR-014**: Stock MUST never go negative, and the total units sold MUST equal the total units scanned in completed transactions, under concurrent completions.
- **FR-015**: A completion interrupted by a crash or lost message MUST be resumed to completion or compensated after restart. No transaction may remain indefinitely in an in-between state.
- **FR-016**: Analytics' reading of scans MUST NOT block or fail any shopper request. Every scan MUST be counted exactly once in effect, including scans made while Analytics was down, and Analytics MUST be able to rebuild its current window from Checkout's scans alone after a restart.
- **FR-017**: Each coordinated operation MUST have bounded time limits per step, so a slow dependency produces a failure or compensation rather than a hang.
- **FR-018**: The chosen coordination style (orchestration or choreography), the order of steps, and the compensation for each step MUST be documented, with the reason for the choice.

**Resilience and observability**

- **FR-019**: Stopping any one microservice MUST NOT break endpoints that do not depend on it, and dependent endpoints MUST fail cleanly in the contract's error shape.
- **FR-020**: Each request MUST carry one correlation identifier that appears in the logs of every microservice it touches, and logs MUST record each coordination step and its outcome.
- **FR-021**: Every microservice MUST expose a health check, and the system MUST start in a safe order (stores, seeding, then microservices, then Gateway).

**Verification and deliverables**

- **FR-022**: An automated boundary check MUST fail on cross-service data-access imports, connections to a data store a service does not own, and compose configuration giving one service another's store credentials.
- **FR-023**: Automated tests MUST cover each completion failure path in User Story 3, including failure injection at each step.
- **FR-024**: A correctness check across all stores MUST verify the invariant (stock sold equals units scanned in completed transactions, no negative stock, no transaction stuck mid-completion, Analytics has read every scan Checkout holds) on a quiescent system.
- **FR-025**: Two load-client JSON reports MUST be produced and committed: default parameters, and stress mode (`--stations=100 --duration=120`), in a subfolder named for spec 007.
- **FR-026**: The README MUST list the microservices, their data stores, the coordination flow (with a diagram), and how to run and test, and MUST state how results compare with spec 005 and why.

### Key Entities *(include if feature involves data)*

- **Catalog Item** (owned by Catalog): a sellable product with SKU, name and price. Fixed during a run.
- **Transaction** (owned by Checkout): a customer's basket with status (open, completing, completed, cancelled).
- **Scanned Item** (owned by Checkout): one scanned unit within a transaction, with the SKU and the price captured when scanned.
- **Completion Record** (owned by Checkout): the durable state of one multi-service completion, recording which steps have finished, so it can be resumed or compensated.
- **Stock Level** (owned by Inventory): units on hand per SKU. Never negative.
- **Stock Reservation / Adjustment** (owned by Inventory): the record that a particular transaction's units were taken, keyed so a retry is recognized and a compensation can reverse it.
- **Low-Stock Alert** (owned by Inventory): raised when stock falls to or below a threshold after a sale.
- **Rolling Scan Window** (held in Analytics' memory, derived): the sequence numbers and SKUs of the most recent scans needed to compute popularity, read from Checkout. Never persisted.
- **Popular-Items Ranking** (held in Analytics' memory, derived): the latest top-selling items over the sliding window.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the existing contract and integration tests pass against the Gateway with no changes to test assertions.
- **SC-002**: The default load run and the stress run (100 stations, 120 seconds) both complete with zero transport failures and zero server errors (only contract-defined business errors such as insufficient stock), and the cross-store correctness check passes after each.
- **SC-003**: In failure-injection tests covering each step of completion, 100% of runs end with all stores consistent (fully completed or fully undone), with no negative stock and no double-applied updates.
- **SC-004**: Retrying any completion 10 times produces the same final state as completing once.
- **SC-005**: Stopping any one microservice leaves 100% success on every endpoint that does not depend on it, and dependent endpoints return a clean error within 5 seconds.
- **SC-006**: With Analytics stopped during a load run and restarted afterward, popular-items reflects all scans made meanwhile within 30 seconds of its recovery.
- **SC-007**: The boundary check reports zero cross-service data-access imports, zero connections to a store the service does not own, and zero shared stores.
- **SC-008**: Any single request can be traced across all microservices it touched using its one correlation identifier.
- **SC-009**: Stress-run throughput (completed transactions per second) is reported next to spec 005's on the same machine. If it is below 70% of that figure, the README explains the measured cause (expected: extra service-to-service calls and coordination, and several stores instead of one).
- **SC-010**: A reader of the README can name every microservice, its data store, and the steps and compensations of the completion flow without reading code.

## Assumptions

- **Granularity stays at four microservices**: the services from spec 005 are kept. The change is in data ownership and coordination, not in the decomposition.
- **Stock moves to Inventory**: in spec 005, Checkout wrote stock in its own database transaction. Under database-per-service, Inventory owns stock and its updates, and that is what makes completion a multi-store operation.
- **Prices are captured at scan time**: Checkout takes the price from a cached snapshot of Catalog's public item list and stores it with the scan. Completion therefore never needs Catalog, and receipts stay stable. The catalog is fixed during a run.
- **Coordination style** (decided in the plan, FR-018): completion is **orchestrated** by Checkout, because the flow has one owner and a fixed order. The scan feed to Analytics is a **pull**: Analytics reads Checkout's scan log by sequence number, so there is no coordination to orchestrate and nothing to compensate.
- **Eventual consistency is acceptable for Analytics and low-stock reporting**, but not for stock levels or the receipt.
- **Payment is simulated and always succeeds**, as in the contract, so there is no payment step to compensate.
- **Container-per-service, one host**: "independently deployable" is met by separate images and separate compose entries, each microservice with its own private store (a database container for Checkout and Inventory, a read-only file volume for Catalog, and in-memory derived state for Analytics). Results are not directly comparable to native runs from weeks 1–4, and the README says so.
- **Docker Desktop is installed** and sized for the stress run. The plan sets recommended CPU and memory.
- **Service-to-service communication uses the microservices' own network interfaces** (synchronous calls and/or a messaging mechanism, to be chosen in planning). Using one is no longer forbidden, unlike spec 005.
- **Reports location**: committed under the load client's reports directory in a subfolder for spec 007.

## Out of Scope

- Any change to the frozen external contract or the load client.
- Orchestration platforms (Kubernetes, Swarm), autoscaling, cross-host deployment, service discovery beyond what compose provides.
- Authentication or TLS at the Gateway or between microservices.
- Real payment processing and its failure paths.
- Splitting into more than four microservices, or merging any of them.
- Re-seeding or schema migration of a running system.
- A native (non-container) launch path.
