# Feature Specification: Service-Based Architecture (Domain Services over a Shared Database)

**Feature Branch**: `005-service-architecture`

**Created**: 2026-10-13

**Status**: Draft

**Input**: User description: "Refactor the self-checkout backend (currently a single process with api/transactions/analytics/db layers plus the streaming analytics pipeline from spec 004) into a services-based architecture of distributed domain services organized by business function. Each service is its own independently deployable unit; all services use a single centralized database; services must not communicate directly unless absolutely necessary — coupling occurs at the database access layer, so shared database access logic must be deliberately packaged and distributed across services. The frozen external HTTP contract must remain unchanged and the unmodified load client (single base URL) must keep working. The submission lists the services defined, and includes two load-client JSON reports: default parameters, and stress mode (`--stations=100 --duration=120`)."

## Overview

Weeks 1–4 delivered one deployable process whose internals were progressively
separated (layers in 002, a pipelined analytics stage in 003, a streaming
pipeline in 004). Week 5 splits that one process into **separately deployable
domain services**, one per business function, that share **one centralized
database** and do not call each other. The external behavior seen by the load
client is unchanged; what changes is how the system is deployed, started, and
reasoned about.

### Services defined by this feature

| Service | Business function | Public endpoints it owns | Background work it owns |
| --- | --- | --- | --- |
| **Catalog Service** | What can be sold, and at what price | `GET /items` | — |
| **Checkout Service** | Running a customer's basket from start to payment | `POST /transactions`, `POST /transactions/{id}/items`, `POST /transactions/{id}/complete`, `GET /transactions/{id}` | Abandoned-transaction sweep; low-stock alert emission at completion |
| **Inventory Service** | Reporting stock levels that need attention | `GET /inventory/low-stock` | — |
| **Analytics Service** | Understanding customer buying behavior | `GET /analytics/popular-items` | Popular-items window computation (the spec 004 streaming pipeline) and its scheduling |

A **Gateway** (edge router) sits in front of the four services on the single
public address the load client targets. It forwards each request by path to
the owning service and contains no business logic; it is infrastructure, not a
domain service.

The **Shared Data Access library** is the one deliberately shared component: a
versioned, packaged library that every service depends on for its database
connection handling and repository functions. It is the only place where
services are coupled.

## Clarifications

### Session 2026-10-13

- Q: How should the "start everything" command work? → A: Docker Compose only, run with Podman (`podman compose`). Every domain service and the Gateway is its own container image. The centralized database and the seed step also run as compose services. All measured load runs go against this compose stack. There is no separate native-process launch path.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Unchanged checkout experience through independently deployed services (Priority: P1)

A checkout station (simulated by the unmodified load client) browses the
catalog, starts a transaction, scans items, completes payment, and looks up
transactions, exactly as before. Behind the single public address, each of
those requests is now served by the domain service that owns that business
function, each running as its own deployable unit against the one shared
database.

**Why this priority**: This is the whole assignment. If the contract or the
graded correctness invariant breaks, nothing else matters. It is also the MVP:
Gateway + Catalog + Checkout already serve the full load-client checkout flow.

**Independent Test**: Run `podman compose up` (which seeds the database and starts every service
and the Gateway), run the existing contract and integration
test suites against the Gateway address, run the load client with default
parameters, then run the correctness-invariant check.

**Acceptance Scenarios**:

1. **Given** a freshly seeded database and all services plus the Gateway running, **When** the load client runs with default parameters, **Then** it completes and writes a JSON report in which every error response is one the contract defines for the situation (for example, insufficient stock at completion once hot SKUs deplete), with no transport failures, timeouts, or 5xx responses.
2. **Given** the same setup, **When** the existing contract test suite runs against the Gateway address, **Then** every test passes unchanged, including every error-catalog response (status code and body shape).
3. **Given** a completed load run, **When** the correctness-invariant check runs, **Then** it reports that the stock sold matches the stock decremented for every SKU.
4. **Given** a scan for an unknown SKU or a malformed transaction id, **When** it is sent through the Gateway, **Then** the response is identical in status and body to the pre-refactor system.

---

### User Story 2 - Each service is its own deployable unit (Priority: P1)

An operator can build, start, stop, and restart any single service without
rebuilding or restarting the others. Each service has its own entry point and
its own container image, and the system is brought up by starting the units,
not by starting one process that contains them all.

**Why this priority**: Independent deployability is the defining property
being graded. It is P1 alongside Story 1.

**Independent Test**: With the compose stack running, stop only the Analytics Service's container (`podman compose stop analytics`).
Catalog and checkout requests keep succeeding through the Gateway. Popular-items
requests fail with a gateway-unavailable response. Restart Analytics and those
requests recover without touching any other unit.

**Acceptance Scenarios**:

1. **Given** all units running, **When** the Analytics Service is stopped, **Then** `GET /items` and the full transaction flow continue to succeed, and only `GET /analytics/popular-items` is unavailable.
2. **Given** the Analytics Service was stopped while scans continued, **When** it is restarted, **Then** it catches up from the shared database and serves a popular-items window that reflects scans recorded while it was down.
3. **Given** a change confined to one service's code, **When** that service is rebuilt and redeployed, **Then** no other service needs to be rebuilt or restarted.
4. **Given** the repository, **When** an operator lists the deployable units, **Then** each of the four services and the Gateway has its own container image and its own compose service entry, and each can be built, started, stopped, and restarted by name.

---

### User Story 3 - Services coordinate only through the shared database (Priority: P2)

A developer can verify that no domain service calls another domain service. Any
cross-domain need is met by reading or writing the shared database through the
Shared Data Access library:

- Checkout prices scanned items by reading catalog data from the database, not by calling the Catalog Service.
- Analytics learns that new scans happened by observing the scan record in the database, not by being notified by Checkout. Today the scan endpoint triggers the recompute in-process; this trigger is removed.
- Inventory reports low stock from the stock and alert records Checkout writes, without calling Checkout.

**Why this priority**: This is the second explicit architectural rule of the
assignment, and the place where the design has to be deliberate. It is P2 only
because Stories 1–2 must hold first for it to be observable.

**Independent Test**: A boundary check (static search plus a runtime check)
confirms that no service's code imports another service's code, and that no
service makes outbound network requests to another service. Then, with the
Gateway bypassed, scanning items directly on the Checkout Service still causes
the Analytics Service's popular-items window to advance.

**Acceptance Scenarios**:

1. **Given** the source tree, **When** the boundary check runs, **Then** it finds zero imports from one service package into another, and zero service-to-service network calls.
2. **Given** Checkout and Analytics running with no direct channel between them, **When** a slide boundary's worth of scans is recorded, **Then** Analytics publishes a new window within the staleness bound (FR-012).
3. **Given** the Shared Data Access library, **When** a developer inspects it, **Then** every table has exactly one owning service with write rights, and other services' access to that table is read-only and explicitly declared.

---

### User Story 4 - Graded load reports for the services architecture (Priority: P2)

The grader opens the repository and finds the two load-client JSON reports
produced against the services architecture: one from a default-parameter run,
and one from the stress run (100 stations for 120 seconds). The submission
text lists the services defined.

**Why this priority**: These artifacts carry 4 of the 5 assignment points, but
they can only be produced once Stories 1–3 work.

**Independent Test**: The reports exist at a documented path in the repository,
are valid JSON produced by the unmodified load client, show the Gateway address
as their base URL, and show the stated station count and duration.

**Acceptance Scenarios**:

1. **Given** a freshly started compose stack (`podman compose down -v` then `podman compose up`), **When** the load client runs on the host with default parameters, **Then** its JSON report is committed under the documented reports path for spec 005.
2. **Given** a freshly restarted compose stack (re-seeded the same way), **When** the load client runs with `--stations=100 --duration=120`, **Then** its JSON report is committed alongside the first and shows 100 stations and a 120-second duration.
3. **Given** the README, **When** the grader reads it, **Then** it lists the four domain services (plus the Gateway as infrastructure), and links both reports.

---

### Edge Cases

- **A service is down**: the Gateway returns a gateway-unavailable error for that service's routes only. Other routes are unaffected. The load client will count these as errors, so measured runs require every unit to be healthy before they start.
- **Analytics starts before any scans exist**: it serves the same empty-window response as today and begins computing once scans appear.
- **Analytics restarts mid-run**: its in-memory window is rebuilt from the most recent scans in the shared database. It never fabricates a window from partial memory.
- **Several Analytics instances run at once**: at most one computes and publishes a given window at a time, through database-level mutual exclusion (as today). The others skip.
- **Several Checkout instances run at once**: completion stays correct because stock locking is enforced by the database, not by in-process state. The abandoned-transaction sweep stays idempotent.
- **Catalog data is read by Checkout and Catalog independently**: both read the same immutable-during-a-run catalog rows, so their views cannot diverge during a run. Re-seeding requires restarting both.
- **Re-seeding while services are running**: not supported (unchanged from today). Re-seeding happens only by bringing the stack down and up again. The seed step runs once per `up`, and domain services do not start until it has completed successfully.
- **Database not yet ready when services start**: services wait for the database container to report healthy and for the seed step to finish. A service that starts against an empty catalog would cache it empty, which is the failure mode today's startup warning exists for.
- **Podman machine not running**: `podman compose up` fails fast with Podman's own error. The README lists `podman machine start` as a prerequisite step.
- **A schema change in the Shared Data Access library**: every service that depends on the changed table must be redeployed against the new library version. This coupling is accepted and documented, not hidden.

## Requirements *(mandatory)*

### Functional Requirements

**Service decomposition and deployment**

- **FR-001**: The system MUST be decomposed into exactly four domain services (Catalog, Checkout, Inventory, Analytics), with the endpoint and background-work ownership given in the Overview table.
- **FR-002**: Each domain service MUST be an independently deployable unit: its own entry point, its own container image, and its own compose service entry, running in its own container. Starting one service (`podman compose up <service>`) MUST NOT start another domain service. It may start the database it depends on.
- **FR-003**: Each service MUST be independently stoppable and restartable without restarting any other unit, and MUST recover its working state from the shared database on start.
- **FR-004**: A Gateway MUST expose the single public address the load client targets (default `http://localhost:8080`) and route each request by path to the owning service. It MUST contain no business logic, validation, or database access.
- **FR-005**: The repository MUST provide one compose file such that `podman compose up` builds all images, starts the centralized database, runs the seed step to completion, then starts all four domain services and the Gateway. `podman compose down -v` MUST tear everything down, including database data, so the next `up` starts from a freshly seeded state. Podman (with a compose provider) is the only runtime prerequisite besides the load client's JDK. Running the services natively is not a supported path.
- **FR-005a**: Every domain service and the Gateway MUST expose a health check that the compose file uses, so `podman compose up` only reports the system ready (and only publishes the Gateway on host port 8080) once every unit is healthy.

**External behavior (unchanged)**

- **FR-006**: Every endpoint, request shape, response shape, status code, and error-catalog entry in the frozen contract MUST behave exactly as before the refactor when called through the Gateway.
- **FR-007**: The correctness invariant (stock sold equals stock decremented, with no oversell) MUST hold after any load run, including the stress run.
- **FR-008**: The unmodified load client MUST run successfully against the Gateway with no changes to its code or flags beyond those named in the assignment.

**Coupling through the database only**

- **FR-009**: Domain services MUST NOT make network calls to one another, and MUST NOT import one another's code. The only shared code is the Shared Data Access library (and contract-neutral utilities such as money formatting and error definitions, which live in that same shared package or a sibling shared package).
- **FR-010**: All services MUST connect to one centralized database instance and schema, which runs as a single compose service. No service may own a private database.
- **FR-011**: The Shared Data Access library MUST declare, for every table, its single owning (writing) service and the services allowed to read it. Write access to a table outside its owner is a boundary violation that the boundary check detects.
- **FR-012**: The Analytics Service MUST detect new scans by observing the shared scan record, with no notification from Checkout, and MUST publish an updated popular-items window no later than 2 seconds after a slide boundary is crossed under default load.
- **FR-013**: The Checkout Service MUST obtain item names and prices for scanning by reading catalog data from the shared database (it may cache it at startup, as today). It MUST NOT obtain them from the Catalog Service.
- **FR-014**: The Inventory Service MUST compute the low-stock report from stock and alert data in the shared database. It MUST NOT obtain it from the Checkout Service.
- **FR-015**: Mutual exclusion that today relies on database locks (stock locking during completion, single-writer window recompute) MUST continue to rely on the database, so it stays correct when a service runs as several processes.

**Verification and deliverables**

- **FR-016**: The repository MUST include an automated boundary check that fails if any service imports another service's code, if any service contains outbound calls to another service, or if any service writes to a table it does not own.
- **FR-017**: The repository MUST contain two load-client JSON reports produced against the compose stack: one with default parameters and one with `--stations=100 --duration=120`. Each is run from the host against `http://localhost:8080` immediately after a fresh `podman compose down -v && podman compose up`.
- **FR-018**: The README MUST list the defined services (with their responsibilities and owned endpoints), describe how shared database access is packaged and distributed, and link both reports.

### Key Entities *(include if feature involves data)*

Entities are unchanged from spec 001. What is new is **ownership**:

| Entity | Owning (writing) service | Readers |
| --- | --- | --- |
| Catalog Item (SKU, name, price) | Catalog (written only by seeding) | Catalog, Checkout, Inventory |
| Inventory Stock (on-hand quantity per SKU) | Checkout (decrements at completion; written by seeding) | Inventory |
| Transaction (status, station, timestamps, totals) | Checkout | — |
| Transaction Item / scan record (the global scan sequence) | Checkout | Analytics |
| Low-Stock Alert (threshold crossings) | Checkout | Inventory |
| Popular Window Snapshot (latest ranked window) | Analytics | Analytics |

- **Shared Data Access library**: the packaged component holding the schema definitions, connection management, and repository functions grouped by owning service, plus the ownership declarations above. Each service depends on it as a library. It is not a running service.
- **Deployable unit**: one service (or the Gateway) together with its entry point, its container image, and its compose service entry.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the existing contract and integration tests pass when pointed at the Gateway, with no changes to test assertions.
- **SC-002**: The default-parameter load run and the stress run (100 stations, 120 seconds) both complete with zero transport failures, timeouts, or server errors (only contract-defined business errors such as insufficient stock), and the correctness invariant passes after each.
- **SC-003**: Stopping any one domain service affects only that service's endpoints. The other services' endpoints keep a 100% success rate during the outage.
- **SC-004**: The boundary check reports zero cross-service imports, zero service-to-service calls, and zero writes outside a table's owner.
- **SC-005**: After a slide boundary is crossed, the popular-items window seen by clients updates within 2 seconds.
- **SC-006**: Stress-run throughput (completed transactions per second) is reported next to the spec 004 stress-run throughput on the same machine. If it is below 80% of that figure, the README explains the measured cause. Expected causes are the added gateway hop and the containerized stack running inside the Podman VM, where spec 004 used native processes and a native database.
- **SC-007**: A reader of the README can name every service, its responsibility, and how services share data, without reading any code.

## Assumptions

- **Gateway is acceptable infrastructure**: the load client accepts one base URL, so a path-routing gateway is needed. It is treated as routing infrastructure, not a domain service, and is not counted in the service list.
- **Four services is the right granularity**: Inventory is split from Checkout because reporting stock health is a separate business function from selling. Stock decrements stay in Checkout, because completion has to lock and decrement stock in one database transaction to keep the invariant. Splitting that write across services would need direct coordination, which the assignment discourages.
- **Seeding is an operator tool, not a service**: the seed script writes catalog and stock directly. It runs before services start and is not a deployable service.
- **Container-per-service on one host, via Podman**: "deployable unit" is met by each service having its own container image and compose entry. The whole system, database included, runs under `podman compose` on the developer's machine, which delegates to an installed compose provider. This deliberately departs from research R3 of 001, where measured runs used a native database. Results are therefore not directly comparable to weeks 1–4, and the README must say so next to the reports.
- **Podman is installed by the operator**: it is not currently installed on the development machine. The README documents a one-time setup (install Podman, `podman machine init`, `podman machine start`). The Podman VM's CPU and memory must be sized for the stress run, and the plan sets the recommended values.
- **The streaming pipeline from spec 004 moves intact into the Analytics Service**: only its trigger changes, from an in-process call on scan to database-observed scan progress.
- **Worker counts per service are a tuning choice** made during planning, set through compose environment variables. The total database connections across all services must stay within the database container's configured connection limit.
- **Reports location**: reports are committed under the load client's reports directory, in a subfolder named for spec 005, so earlier weeks' reports stay distinguishable.

## Out of Scope

- Splitting the database per service, event buses, message queues, or any asynchronous messaging between services.
- Service discovery, orchestration platforms (Kubernetes, Swarm), autoscaling, or cross-host deployment.
- A native (non-container) launch path for the services.
- Authentication or TLS at the Gateway.
- Any change to the frozen external contract or to the load client.
