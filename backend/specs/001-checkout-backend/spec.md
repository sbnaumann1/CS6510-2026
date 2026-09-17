# Feature Specification: Supermarket Self-Checkout Backend API

**Feature Branch**: `001-checkout-backend`

**Created**: 2026-09-16

**Status**: Draft

**Input**: Build a supermarket self-checkout backend API that implements the OpenAPI specification at spec.yaml. The system must handle concurrent transactions from up to 100 checkout stations, track inventory (2000 items with 10000 units stock each), process item scans, complete transactions with payment, decrement inventory, and provide analytics including low-stock alerts and popular items tracking with a sliding window. Performance target: sub-1ms latencies. Tech stack: FastAPI, PostgreSQL, SQLAlchemy ORM.

## User Scenarios & Testing

### User Story 1 - Customer Completes Checkout Transaction (Priority: P1)

A customer at a self-checkout station starts a transaction, scans multiple items (1-20 units), then completes the transaction to finalize their purchase. The system must process each scan and transaction completion quickly and accurately, updating inventory only when payment is complete.

**Why this priority**: This is the core business function. Every customer checkout depends on this workflow. Without it, the system is non-functional.

**Independent Test**: Can be fully tested by simulating a single customer checkout through all three API operations (start → scan → complete) and verifying that: (1) transaction is created with correct ID, (2) each item scan is recorded with correct price, (3) inventory is decremented by the correct amount upon completion, (4) receipt is generated with accurate totals.

**Acceptance Scenarios**:

1. **Given** a new transaction is started at a station, **When** the API processes the start request, **Then** the system returns a unique transaction ID and status "OPEN"
2. **Given** an open transaction with items scanned, **When** an item is scanned, **Then** the system returns the item's price and updates the running total
3. **Given** an open transaction with 5 items scanned, **When** the transaction is completed, **Then** the system processes payment, decrements inventory for each item, and returns a receipt with itemized breakdown
4. **Given** a completed transaction, **When** a customer attempts to scan another item, **Then** the system rejects the request with error "transaction not open"

---

### User Story 2 - Inventory Management & Low-Stock Alerts (Priority: P2)

Store managers need to monitor inventory levels and receive alerts when stock falls below configurable thresholds. The system must track purchases in real-time, prevent overselling, and provide a queryable list of low-stock items.

**Why this priority**: Prevents inventory inaccuracy and stockouts. Critical for store operations but doesn't block checkout if temporarily unavailable.

**Independent Test**: Can be tested independently by: (1) starting inventory at 100 units, (2) completing 60 transactions of 1 item each, (3) querying low-stock endpoint with threshold=50, (4) verifying the alert list shows the item with current stock=40.

**Acceptance Scenarios**:

1. **Given** an item with stock=51 and low-stock threshold=50, **When** one unit is purchased, **Then** a low-stock alert is generated for that item
2. **Given** a query for low-stock items with threshold=100, **When** the endpoint is called, **Then** the system returns all items with current stock < 100 in timestamp order
3. **Given** any completed transaction, **When** stock would go negative, **Then** the system prevents the transaction and returns error "insufficient stock"

---

### User Story 3 - Popular Items Analytics with Sliding Window (Priority: P3)

Analytics system tracks the most frequently scanned items in a sliding window (e.g., most recent 1000 scans) and provides ranked list of top items. Results are updated periodically and queryable on demand.

**Why this priority**: Provides business intelligence for merchandising and marketing. Less critical than core checkout or inventory management but valuable for store operations.

**Independent Test**: Can be tested independently by: (1) scanning 1500 items with distribution where SKU-001 appears 150 times, SKU-002 appears 100 times, etc., (2) querying popular items with limit=10, (3) verifying top result is SKU-001 with scanCount=150.

**Acceptance Scenarios**:

1. **Given** a sliding window tracking the last 1000 scans, **When** scan #1001 occurs, **Then** scan #1 is removed from the window and the popular items list is recomputed
2. **Given** a popular items query, **When** the endpoint is called, **Then** the system returns top N items ranked by scan count in the current window with window metadata (start, end, computed timestamp)

---

### Edge Cases

- What happens when a customer's transaction is interrupted (network failure mid-checkout)? System allows grace period for transaction recovery or mark as abandoned after timeout.
- How does the system handle stock depletion during concurrent checkouts? Last-one-wins: first transaction to complete and decrement inventory succeeds; second fails with insufficient stock error.
- What happens if popular-items sliding window has fewer than N items in current window? Return all available items (less than N).
- How does the system behave under extreme concurrent load (100+ stations simultaneously)? Maintains data consistency; may see increased latency but no data corruption or overselling.

## Requirements

### Functional Requirements

- **FR-001**: System MUST accept HTTP POST `/transactions` request with stationId and return unique transactionId with status "OPEN"
- **FR-002**: System MUST accept HTTP POST `/transactions/{txId}/items` with SKU, return item details (name, price), and maintain running transaction total
- **FR-003**: System MUST accept HTTP POST `/transactions/{txId}/complete` and finalize transaction (decrement inventory for all scanned items, return receipt)
- **FR-004**: System MUST NOT allow stock to go negative; if insufficient stock exists for any item in a transaction, completion fails with 409 error
- **FR-005**: System MUST decrement inventory ONLY at transaction completion, not at scan time
- **FR-006**: System MUST generate low-stock alerts when any item's stock drops below a configurable threshold (default: 50)
- **FR-007**: System MUST accept HTTP GET `/inventory/low-stock` and return list of items below threshold with current stock, threshold, and alert timestamp
- **FR-008**: System MUST track all item scans globally and compute sliding window (default: 1000 item window, recomputed every 500 scans)
- **FR-009**: System MUST accept HTTP GET `/analytics/popular-items?limit=10` and return top N items by scan count in current window with window metadata
- **FR-010**: System MUST accept HTTP GET `/items` and return full catalog (2000 items) with SKU, name, and price
- **FR-011**: System MUST handle up to 100 concurrent checkout stations without data loss or incorrect stock counts
- **FR-012**: System MUST support all error scenarios defined in OpenAPI spec (404 for not found, 409 for conflict, 400 for invalid input)

### Key Entities

- **CatalogItem**: Represents a product in store inventory. Attributes: SKU (unique identifier), name, price. Immutable after creation.
- **Transaction**: Represents a customer's checkout session. Attributes: transactionId (unique), stationId, status (OPEN/COMPLETED/CANCELLED), itemCount, runningTotal, startedAt, completedAt. Mutable until completion.
- **TransactionItem**: Individual line item in a transaction. Attributes: transactionId (foreign key), SKU (foreign key), quantity (unit count), unitPrice. Immutable once recorded.
- **InventoryStock**: Current stock level for each catalog item. Attributes: SKU (foreign key), currentStock (integer), initialStock (reference), version (for concurrency control). Mutable on transaction completion.
- **LowStockAlert**: Generated event when stock drops below threshold. Attributes: SKU, currentStock, threshold, triggeredAt timestamp. Immutable historical record.
- **PopularItemWindow**: Computed analytics result for sliding window. Attributes: SKU, rank (1-N), scanCount, windowStart (global scan sequence), windowEnd, computedAt timestamp.

## Success Criteria

### Measurable Outcomes

- **SC-001**: API endpoints respond with p50 latency < 0.5ms, p95 < 1.0ms, p99 < 2.0ms (measured from 10+ concurrent stations over 60+ seconds)
- **SC-002**: System processes 2,000+ transactions per second with 10 stations; scales to handle 100+ stations without transaction failure
- **SC-003**: Stock accuracy maintained: final inventory count + sum of all decrements = initial count, even under concurrent load from 100 stations
- **SC-004**: Zero data loss: all completed transactions are recorded; no duplicate inventory decrements
- **SC-005**: Low-stock alerts generated within 100ms of inventory drop below threshold
- **SC-006**: Popular items rankings reflect true scan counts within 500-scan update interval (sliding window)
- **SC-007**: Load test with default parameters completes all transactions with 0% error rate
- **SC-008**: Stress test (100 stations, 120 seconds) completes with 0% transaction failures and correct final stock counts

## Assumptions

- **Inventory model**: All 2000 catalog items exist at initialization with 10,000 units in stock each; catalog is immutable during test run
- **Concurrent access**: System uses pessimistic locking (row-level locks) on inventory during transaction completion to ensure correctness over max throughput
- **Payment processing**: Payment is always successful (no failed payment scenarios); simulated as instant
- **Data persistence**: All data (catalog, transactions, inventory, alerts, analytics) must be persisted to PostgreSQL and survive server restarts
- **Analytics window**: Sliding window uses fixed parameters (1000-item window, 500-scan slide interval); recomputation is periodic, not real-time per scan
- **Error handling**: All OpenAPI-specified error codes and messages must be implemented; HTTP 2xx = success, 4xx = client error, no 5xx errors expected under normal load
- **Session recovery**: If a checkout station connection is lost mid-transaction, the transaction is marked abandoned after configurable timeout (suggest 5 min) and inventory is not decremented
- **Load test harness**: Java load-client drives all testing; API must match OpenAPI spec exactly for client to function
