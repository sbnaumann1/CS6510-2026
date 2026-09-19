## Architecture Characteristics: Requirements

### Brainstorm
Availability: The server and database should be available to customers at all operational hours (through the duration of the test). Errors or client failures at one checkout station (thread) should not interrupt the availability for other customers. With the current monolithic architecture, a failure will bring everything down. This robustness will be a key focus in the coming weeks.

Continuity: The system must be able to restore catalog stock, transaction metadata, and analytics data after a restart or failure so the next test run starts in a consistent state. Recovery should be a couple of minutes at most, since an hour of downtime means closing the lanes. We also accept losing under a second of already-committed transactions if the machine itself dies, because we run with synchronous commit turned off to keep throughput up. That is a deliberate trade, not an accident.

Performance: Latency should be invisible to the customer rather than just technically fast. Scanning is the operation that repeats, so it carries the tightest budget, while completion can be looser because it does several database round trips under locks where a scan does one. Under the default 10-station workload, no operation's p95 should exceed about 200% of its own median, and completion's p95 should stay within roughly 300-400% of scan's. Nothing should ever take long enough that a customer starts looking for staff. The only errors we accept are from the business logic (like stock running out); faults should be 0% of requests.

Recoverability: If the app crashes mid-run, it must restore persisted inventory and analytics quickly without manual repair. Open transactions are intentionally not recovered, since they hold no stock (inventory only moves at completion) and get cancelled automatically after a timeout. In future versions, we might even want to be able to restore the current state of transactions so customers can get up and running mid interaction.

Reliability: Stock must never go negative, and for every SKU the total sold quantity plus final inventory must equal the initial inventory. Stock must also never be silently clamped at zero, which is the sneaky version of the same bug. Concurrent completions must preserve correctness. No customer should be able to check out (and pay for) a full basket that was only partially available. This is the one characteristic with 0% tolerance: a system that fails it is wrong no matter how fast it is.

Robustness: The API must handle invalid transaction IDs, missing SKUs, empty baskets, and malformed requests without corrupting state or crashing the service. If two transactions contend for the same SKU, the outcome should be deterministic rather than racy: one of them completes and the other gets a clear conflict error, with no deadlock and no overselling.

Scalability: The architecture must support increased concurrency from 10 stations to 100+ stations while maintaining correctness and avoiding disproportionate latency growth. Going to 100 stations is 10x the load, so p95 latency growing up to about 1000% is proportional and acceptable. Growth meaningfully beyond that means something is serializing. Throughput is allowed to drop under stress, but correctness is not allowed to change by any amount.

Configurability: The low-stock threshold, popular-item window size, slide interval, stock levels, worker count, and database configuration should be set through environment variables or config files without changing the core code.

Extensibility: New business rules, inventory attributes, customer interactions, additional analytics outputs, or other endpoints should be easy to add without rewriting the existing transaction or inventory core.

Installability: The system should be straightforward to install and start locally, including the database and required runtime dependencies, so the app can be run consistently in development and testing. It should also be easy and consistent to refresh to a clean state without error, since every measured run depends on starting from identical state.

Maintainability: The code should isolate the most error-prone areas —> transaction lifecycle management, inventory decrement logic under concurrency, and sliding-window analytics—into separate modules (classes/modules at beginning) or services (later in the semester) with clear boundaries, so updates to one area do not require rewriting the rest of the backend. The implementation should make it easy to reason about how stock is decremented, how duplicate scans are prevented, and how popularity counts are recalculated.

Upgradeability: The system should support schema changes and logic updates through migration or replacement steps without manual database repair. Right now the ORM models are the source of truth and the seed script rebuilds from scratch, which is fine for a benchmark that resets before every run but would not survive real data. Ideally, we will use ORM, LinkML, and Alembic to assist with this robust upgrade functionality.

Security: Customers should never have access to eachother's purchase information -- enforced at the API level. The contract current defines no authentication, we serve plaintext HTTP on localhost, and nothing is encrypted at rest. That is acceptable for week 1 benchmarking. As we expand our architecture, possibly over networks, we will need to adjust this. 

Supportability: The application should emit structured logs and error information for transaction failures, stock errors, and analytics issues to make debugging under load straightforward. A legitimate error (stock ran out) has to be distinguishable from a fault (the service broke), otherwise a report full of conflict errors is unreadable.

Usability/achievability: The implementation should be easy to start locally, reset between test runs, and debug so it can be validated quickly without a long setup process.

### Group and DeDuplicate
#### Category Groupings: 
Operational:
- Availability
- Continuity 
- Performance
- Recoverability
- Reliability
- Robustness
- Scalability

Structural: 
- Configurability
- Extensibility
- Installability
- Maintainability
- Upgradeability

Cross-Cutting:
- Security
- Supportability
- Useability (merged into Installability below)

#### Deduplicated
- Installability + Usability/achievability: these were brainstormed separately but state the same requirement, namely that the system be quick to install, reset to a clean state between runs, and debug locally. They collapse into one characteristic, carried forward as Installability.

#### Implementation Groupings (what will benefit from eachother)
- Continuity, Recoverability, Upgradeability
- Availability, Scalability, Performance
- Reliability, Robustness, Supportability
- Installability (incl. Usability/achievability), Configurability

### Trade Offs
- Performance vs. reliability: Every database lock and check ensures reliability, but slows the system down. The most extreme version of this is visible in the mockserver that just decrements without any db backing. Very fast, but not reliable! It runs a bit over 3x our throughput and silently loses inventory the moment anything sells out. Paying that 3x for correctness is obviously the right call, since a checkout that charges for stock it does not have is not a faster system, it is a broken one.
- Scalability vs. simplicity: More workers or more complex concurrency controls can improve throughput, but they add operational and debugging complexity. Similarly, asynchronous workers are mandatory, but async is harder to fully grok in code than standard procedural operations.
- Latency vs. durability: Small scale, highly specific tradeoffs also exist like turning off Postgres' synchronous_commit feature, which may not save the latest committed transactions. This increases speed by not waiting for the data to be written to disk before acknoledging a commit. But if there is a major crash during that transaction, the commits would be rolled back and those transaction details might be lost.
- Maintainability vs. optimization vs. supportability: Delegating out complex functionality to well designed packages by people who have more research experience on the function will increase speed over writing things ourselves, but maintaining packages can be a tax on code and make it harder to ship a package out.

### Top Ranked 3
1. Reliability/safety: Inventory correctness, transaction consistency, and data security are the most important business requirements. If customers pay for things they don't get, have their data stolen, or have erroneous transactions, this could lead to shutting down the entire operation (devastating). It is also the only one of these with no acceptable margin, and the one we knowingly tradeoff performance in order to guarantee. Inventory correctness and transaction consistency are enforced now, while the data-security half of this priority is a future commitment, with the authentication, transport security, and secret management described above to be implemented in the coming weeks.
2. Performance: The service must handle concurrent station traffic and keep latency acceptable under the client's load profiles. This is purely a customer satisfaction concern. Customers will wait for a period of time, but too much of this will lead to dissatisfaction. Right now we clear this target with a lot of room, so it is not the binding constraint this week, but it is the first thing that will erode once services start talking over a network.
3. Scalability: The design should handle higher station counts without major redesign or large latency increases. We want to be able to grow the business, but small-scale, excellent service can still make money. The completion slowdown we already see at 100 stations is an issue that we should address in future weeks.
