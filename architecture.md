## Architecture Characteristics: Requirements

### Brainstorm
Availability: The server and database should be available to customers at all operational hours (through the duration of the test). Errors or client failures at one checkout station (thread) should not interrupt the availability for other customers.

Continuity: The system must be able to restore catalog stock, transaction metadata, and analytics data after a restart or failure so the next test run starts in a consistent state (absolute maximum is 1hr of downtime).

Performance: Under the default 10-station workload, the system should complete transaction start, scan, and completion requests in under 1 second for the median request and under 2 seconds for the p95 request; under stress mode (100 stations for 120 seconds), the system should remain responsive with no severe latency spikes and should sustain throughput high enough to complete the test without timing out requests.

Recoverability: If the app crashes mid-run, it must restore persisted inventory and analytics quickly without manual repair. In future versions, we might even want to be able to restore the curent state of transactions so customers can get up and running mid interaction.

Reliability: Stock must never go negative, and for every SKU the total sold quantity plus final inventory must equal the initial inventory; concurrent completions must preserve correctness. No customer should be able to check out (and pay for) a full basket that was partially availabile.

Robustness: The API must handle invalid transaction IDs, missing SKUs, empty baskets, and malformed requests without corrupting state or crashing the service. If two transactions attempt to access the same SKU (or even the same exact item) and there is still inventory, then another of same characteristics should be provided rapidly to one customer.

Scalability: The architecture must support increased concurrency from 10 stations to 100+ stations while maintaining throughput and avoiding latency spikes greater than 200% of the baseline average latency for any operation during the test. In practice, the p95 latency during stress mode should remain within 2x the average latency observed during the default 10-station run, and no operation should experience a sustained spike beyond that threshold.

Configurability: The low-stock threshold, popular-item window size, slide interval, and database configuration should be set through environment variables or config files without changing the core code.

Extensibility: New business rules, inventory attributes, customer interactions, additional analytics outputs, or other endpoints should be easy to add without rewriting the existing transaction or inventory core.

Installability: The system should be straightforward to install and start locally, including the database and required runtime dependencies, so the app can be run consistently in development and testing. It should also be easy and consistent to refresh to a clean state without error.

Maintainability: The code should isolate the most error-prone areas —> transaction lifecycle management, inventory decrement logic under concurrency, and sliding-window analytics—into separate modules (classes/modules at beginning) or services (later in the semester) with clear boundaries, so updates to one area do not require rewriting the rest of the backend. The implementation should make it easy to reason about how stock is decremented, how duplicate scans are prevented, and how popularity counts are recalculated.

Upgradeability: The system should support schema changes and logic updates through migration or replacement steps without manual database repair. Ideally, we will use ORM, LinkML, and Alembic to assist with this robust upgrade functionality.

Security: Customers should never have access to eachother's purchase information -- enforced at the API level. The system must protect data in transit and at rest with secure defaults, avoid storing secrets in code, and restrict access to sensitive management functionality.

Supportability: The application should emit structured logs and error information for transaction failures, stock errors, and analytics issues to make debugging under load straightforward.

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
- Installabiltiy
- Maintainability
- Upgradability 

Cross-Cutting:
- Security
- Supportability
- Useability

#### Implementation Groupings (what will benefit from eachother)
- Continuity, Recoverability, 
- Availability, Scalability, Performance
- 

### Trade Offs
- Performance vs. reliability: Every database lock and check ensures reliability, but slows the system down. The most extreme version of this is visible in the mockserver that just decrements without any db backing. Very fast, but not reliable!
- Scalability vs. simplicity: More workers or more complex concurrency controls can improve throughput, but they add operational and debugging complexity. Similarly, asyncronous workers are mandatory, but async is harder to fully grok in code than standard procedural operations.
- Maintainability vs. optimization vs. supportability: Delegating out complex functionality to well designed packages by people who have more research experience on the function will increase speed over writing things ourselves, but maintaining packages can be a tax on code and make it harder to ship a package out.

### Top Ranked 3
1. Reliability/safety: Inventory correctness is the most important requirement because incorrect stock updates break the core business rule of the system.
2. Performance: The service must handle concurrent station traffic and keep latency acceptable under the client’s load profiles.
3. Scalability: The design should handle higher station counts without major redesign or large latency increases.