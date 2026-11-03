# Architecture comparison: the self-checkout system, six architectures over six weeks

> **Status: DRAFT.** Sections 0–2 are filled from committed evidence and a metrics run on
> 2026-11-01. Section 3's star ratings are the author's judgement, checked against §1 and §2.
> The open items under §1 still need confirming.

## 0. The implementations

Six architectures, one per week. Some weeks also include a later optimization pass on the same
architecture (same style, tuned code). Those are not separate architectures and are reported
under their parent.

| Week | Architecture (FoSA style) | Initial spec | Later optimization of the same architecture | Source |
| --- | --- | --- | --- | --- |
| 1 | Monolith | 001 | n/a | branch `001-checkout-backend` |
| 2 | Layered | 002 | n/a | `refactor/002-layered-architecture` |
| 3 | Pipelined analytics (pipeline) | 003 | 004 streaming analytics | `feature/pipelined-analytics`, merges `6a92f7b`, `7155890` |
| 4 | Service-based: four domain services, nginx gateway, one shared PostgreSQL | 005 | 006 hot-path latency | `005-service-architecture`, `006-hot-path-latency` |
| 5 | Microservices: private store per service, saga over HTTP | 007 | n/a | `007-microservices-architecture` |
| 6 | Event-driven microservices: Kafka, outbox, idempotent consumers | 008 | 009 Kafka optimization | merge `ff7b112`, branch `009-kafka-optimization` (tip `b6c4a1b`) |

> Numbering note: the load-client reports README and the spec folders number by *spec*
> (e.g. "Week 8" = spec 008). This document numbers by *architecture week*. Where a report is cited
> its original filename is given so it can be traced.

## 1. Performance evidence

All numbers are from the committed load-client JSON reports (summary extracted by script from git
history). Same load client, default config is 10 stations / 60 s, stress is 100 stations / 120 s,
stock 10 000 per SKU unless marked "clean" (2 000 000 per SKU, nothing depletes).

**Reading the numbers.** With stock 10 000 the hot SKUs sell out and later completions are
refused with `409 INSUFFICIENT_STOCK`. That is correct behaviour (the invariant is the graded
check) but it inflates tx/s because refusals are cheap. Compare like with like: depleting runs
with depleting runs, clean runs with clean runs. Absolute numbers also drift with host state, for
example the battery-power note on the 006 optimization runs (week 4).

### Depleting runs (stock 10 000), the common basis

| Week | Architecture | Variant | Default tx/s | Stress tx/s | Stress COMPLETE p95 | Reports |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Monolith | as built | 328.4 | 276.2 | 74.6 ms | `20260918-170238`, `-170702` |
| 2 | Layered | as built | not measured at default stock (see clean runs) | | | |
| 3 | Pipelined | 003 as built | 336.9 | 271.8 | 73.9 ms | `20260929-202454`, `-202733` |
| 3 | Pipelined | 004 streaming (native processes) | 320.9 | 269.1 | 63.9 ms | `20261002-163856`, `-164059` |
| 4 | Service-based (Docker) | 005 as built | 220.6 | 190.9 | 225.0 ms | `005-…/20261013-093301`, `-093519` |
| 4 | Service-based (Docker) | 006 optimized | 262.5 | 224.5 | 121.6 ms | `006-…/20261016-082652`, `-082910` |
| 5 | Microservices | 007 as built | 200.5 | 196.2 | 175.7 ms | `007-…/20261023-104702`, `-104920` |
| 6 | Event-driven | 008 as built | 166.2 | 160.2 | 303.1 ms | `008-…/20261027-220244`, `-220513` |
| 6 | Event-driven | 009 optimized | 180.3 | 168.0 | 268.9 ms | `009-…/20261101-173942`, `-174211` |

**Week 6 same-session controls.** The host was about 4 % slower on 2026-11-01 than on 10-27 (the
unchanged 008 code scored lower than its own report), so the 009 README re-ran the 008 code the
same day (`009-kafka-optimization/controls/`):

| | 008 report (10-27) | 008 re-run (11-01) | 009 with producer linger 2 ms | **009 final** |
| --- | --- | --- | --- | --- |
| Default tx/s | 166.2 | 160.2 | 170.5 | **180.3** |
| Stress tx/s | 160.2 | 153.0 | 163.4 | **168.0** |
| Stress COMPLETE mean | 183.3 ms | 163.4 ms | 174.9 ms | **162.1 ms** |

Like for like (same session), 009 is **+12.5 % tx/s at 10 stations and +9.8 % at 100**, and +26 % /
+20 % in items scanned per second. The 009 change was a raw-ASGI scan fast path on a bare asyncpg
pool (Checkout CPU per request about −30 %), `linger_ms=0` on Checkout's producer, reply
filtering by record key, and an nginx upstream keep-alive limit. Inventory is unchanged.

### Clean runs (stock 2 000 000), monolith family only

| Week | Default tx/s | Stress tx/s | Stress COMPLETE p95 |
| --- | --- | --- | --- |
| 1 Monolith (V7 clean) | 788.0 | not run | |
| 2 Layered | 637.5 | 832.4 | 143.5 ms |
| 2 A/B (monolith vs layered, 09-23) | 741 vs 733 (per reports README) | n/a | n/a |
| ? `20260928-173337/-173540` (clean, purpose unconfirmed) | 731.7 | 838.7 | 142.0 ms |

The week 2 refactor cost no measurable throughput. The A/B in
`backend/specs/002-layered-architecture/reports/` puts it within noise.

### Reading across the weeks

1. **Weeks 1–3 (monolith, layered, pipelined): about 270–335 tx/s, p95 COMPLETE about 64–75 ms.**
   Layering and pipelining are essentially free at run time. Week 3's 004 streaming refinement
   moved the stress COMPLETE p95 from 73.9 to 63.9 ms.
2. **Week 4, going to services in Docker: −31 % default, −29 % stress vs the 004 baseline.**
   The spec 006 investigation (`specs/006-service-perf-investigation/investigation.md`) attributes
   it as about 44 % Docker port forwarding, about 52 % VM overhead and about 0 % from nginx or the
   service code. It is largely the deployment platform, not the architectural style. The same code
   natively served 827 tx/s against the monolith's 818 in a same-session check. The 006
   optimization (one-statement critical section) then recovered +19 % / +18 %: under a hot-row
   lock in a closed-loop client, per-hop latency multiplies.
3. **Week 5, microservices:** 91 % / 103 % of 005, 76 % / 87 % of 006. A private store per
   service turns one transaction into three local transactions plus an HTTP hop (saga).
4. **Week 6, event-driven:** 83 % / 82 % of week 5. COMPLETE mean doubles (10.2 → 21.6 ms
   default, 80 → 183 ms stress). Inventory's own work is about 10–20 ms. The rest of the ~150 ms
   wait is consumer queueing plus two broker hops. The 009 optimization recovers 8 % / 5 %
   against the 008 report (12.5 % / 9.8 % same-session), reaching 90 % / 86 % of week 5's
   throughput.
5. **Each optimization pass recovered some throughput, but none caught the previous
   architecture.** The ordering is stable at both loads: weeks 1–3 (about 270–335 tx/s) >
   week 4 optimized (262 / 224) > week 5 (200 / 196) > week 6 optimized (180 / 168). Every
   step toward independent deployment and failure isolation cost throughput on one host.
6. **Correctness held in every run.** The stock invariant passed on every graded run, including
   the cross-store version from week 5 on.

> Open items to verify before submitting: the label for `20261002-092147` (91.3 tx/s, 82.7 %
> COMPLETE errors) looks like a bad run and `006df17` re-ran it ("clean seeding"); confirm that
> `1002-163856/164059` is the 004 streaming result (the README calls it the "Week 4 baseline" for
> spec 005), and decide where `1002-092147/092724/094043/094600/152414/152618` belong (clean
> re-runs and intermediate 003/004 runs).

## 2. Code metrics

### Method

Each week's final state was exported with `git archive` (no checkout, no build) into a scratch
directory: production Python only, no tests. The ref per week: week 1 `001-checkout-backend`
(`backend/app`), week 2 `refactor/002-layered-architecture` (`backend/app`), week 3 `7155890`
(003 + 004 streaming, `backend/app`), week 4 `006-hot-path-latency`, week 5
`007-microservices-architecture`, week 6 `009-kafka-optimization` (`services/`, `packages/`,
`gateway/`, `events/`). Tools ran one snapshot at a time at `nice -n 19`
(`backend/scripts/analysis/analyze.py`, using radon 6.0.1 and lizard as libraries).

| Metric | Tool | Why it matters here |
| --- | --- | --- |
| SLOC, files, classes, functions | `radon.raw`, `lizard` | Size baseline |
| Cyclomatic complexity (mean, max, blocks > 10) | `radon.complexity` | Local complexity |
| Maintainability index (mean, min) | `radon.metrics.mi_visit` | Aggregate maintainability |
| Ce, Ca, instability I = Ce / (Ca + Ce) per component | AST import graph (own script) | **Coupling** |
| Wire-level coupling: peer URLs, topics, producer → consumer pairs | grep of config and `events/topics.yaml` | Coupling the import graph cannot see |
| LCOM4 per class (connected components of methods linked by shared `self` attributes or calls) | AST (own script) | **Cohesion** |

A *component* is the unit whose dependencies are counted. In weeks 1–3 it is a module or
sub-package under `app/` (12 of them). In weeks 4–6 it is a Python package: one per service plus
the shared libraries (`checkout_data`, `checkout_web`). Ca and Ce count distinct *other*
components imported, so cross-service coupling shows up only where code is shared.

### Size, complexity, maintainability

| Week | Architecture | Files | SLOC | Classes | Funcs | Mean CC | Max CC | Blocks CC > 10 | MI mean | MI min |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Monolith | 19 | 899 | 23 | 50 | 1.75 | 13 | 1 | 81.1 | 57.4 |
| 2 | Layered | 25 | 969 | 23 | 67 | 1.59 | 11 | 1 | 86.9 | 61.8 |
| 3 | Pipelined | 26 | 1 207 | 33 | 78 | 1.94 | 11 | 1 | 85.2 | 56.8 |
| 4 | Service-based | 36 | 1 528 | 23 | 106 | 2.19 | 12 | 1 | 84.8 | 41.0 |
| 5 | Microservices | 54 | 2 614 | 50 | 210 | 2.41 | 14 | 5 | 79.9 | 41.2 |
| 6 | Event-driven | 72 | 4 247 | 69 | 361 | 2.35 | 15 | 6 | 79.0 | 17.8 |

- **Size grows 4.7× from week 1 to week 6** (899 → 4 247 SLOC), and function count 7×. Most
  of the growth is plumbing, not domain logic: week 5 adds the saga and HTTP clients, week 6 the
  outbox, relay, bus, consumers and event schemas. Week 6 is 62 % larger than week 5.
- **Complexity per function stays low** (mean CC 1.6–2.4), so no week is "hard to read" function by
  function. The tail grows: blocks above CC 10 go from 1 to 6, with the worst in week 6
  (`MemoryBus.publish` CC 15, `KafkaConsumer._ensure_started` 13, `saga.complete` 14).
- **Week 2's layering improved maintainability** (MI 81.1 → 86.9, mean CC 1.75 → 1.59, worst
  function 13 → 11) at no run-time cost (§1). It is the best-scoring week.
- **Week 6 has the one genuinely poor file:** `checkout_web/bus.py` has MI 17.8 (grade B,
  the only file below 40), which is the broker abstraction (Kafka and an in-memory fake) in the
  shared library.

### Coupling

**Import-level (Ce / Ca / I):**

| Week | Components | Import edges | Mean I | Notes |
| --- | --- | --- | --- | --- |
| 1 | 12 | 22 | 0.38 | `api` and `main` depend on 6 others each; `services` (360 SLOC) is one big module; `config`, `errors`, `models` are stable (I = 0) |
| 2 | 12 | 23 | 0.39 | `services` is split into `transactions` (165 SLOC) and `analytics` (104) and the DB code grows (37 → 227). `api` still imports `db` directly, so the layer boundary is not strict at import level |
| 3 | 12 | 23 | 0.39 | `analytics` grows 104 → 326 SLOC but keeps the same 3 outgoing edges and one dependant (`api`) |
| 4 | 6 | 8 | 0.67 | **Every service imports `checkout_data` (622 SLOC, Ca 4, I 0) and `checkout_web`** |
| 5 | 5 | 4 | 0.80 | `checkout_data` is gone; each service imports only `checkout_web` |
| 6 | 5 | 4 | 0.80 | Same edges, but `checkout_web` is now 1 053 SLOC |

**Where the code lives (SLOC):**

| Component | Wk 4 | Wk 5 | Wk 6 |
| --- | --- | --- | --- |
| checkout service | 228 | 954 | 1 514 |
| inventory service | 49 | 605 | 774 |
| analytics service | 452 | 616 | 674 |
| catalog service | 48 | 158 | 232 |
| shared `checkout_data` | 622 | – | – |
| shared `checkout_web` | 129 | 281 | 1 053 |
| **shared share of all code** | **49 %** | **11 %** | **25 %** |

Reading it:
- **Week 4's "services" are thin façades over a shared data layer.** Inventory and Catalog are
  48–49 SLOC each. The domain code is in `checkout_data`, which all four services import and which
  fronts one shared PostgreSQL. The import graph shows coupling at 49 % of the code, matching the
  shared database in the design. This is the service-based style, not microservices.
- **Week 5 removes the shared data layer.** Shared code falls to 11 %, and each service owns its
  logic and store. The coupling does not vanish; it moves to the wire (below).
- **Week 6 moves it into the messaging layer.** The import graph is unchanged (4 edges), but
  the shared library grows 3.7× to 25 % of the code (bus, event envelope and schemas, consumer
  loop). Every service must upgrade together with that library, so the independence of week 5 is
  partly traded for a common messaging kernel. This is the design cost of event-driven systems with
  a shared envelope, and it is visible only because `checkout_web` is measured.
- Weeks 1–3 have I around 0.4: a stable core (`config`, `errors`, `models`, `money`, I = 0) and
  volatile entry points (`api`, `main`, I ≥ 0.86). That is the healthy shape. Layering (week 2)
  and the pipeline (week 3) barely changed the edge count (22 → 23 → 23). Week 2 split the
  monolithic `services` module, and week 3's new analytics code stayed inside one component.

**Wire-level coupling (what the import graph cannot see):**

| Week | Cross-service channel | Count | Evidence |
| --- | --- | --- | --- |
| 4 | Shared PostgreSQL | 1 database, 4 services | `compose.yaml`, `checkout_data` |
| 4 | Peer HTTP calls | 0 | no peer URL in any service config |
| 5 | Peer HTTP calls (configured URLs) | 5 edges: checkout → catalog, inventory; inventory → catalog; analytics → catalog, checkout | `*/config.py` |
| 5 | Databases | 2 private (+ read-only catalog file, none for analytics) | `compose.yaml` |
| 6 | Peer addresses | 0 | no peer URL in any service config |
| 6 | Topics (plus dead letter) | 4 | `events/topics.yaml` |
| 6 | Producer → consumer pairs | 6 (catalog → 3 consumers, checkout → analytics, checkout → inventory, inventory → checkout) | `events/topics.yaml` |
| 6 | Event schemas | 12 JSON schemas | `events/schemas/` |

Week 6 has no service that knows another service's address, which is the decoupling the
architecture buys. In exchange, 12 schemas and a topic map are now the contract, and the
`checkout ↔ inventory` pair is a request/reply cycle over two topics (a cycle in the dependency
graph, handled by the saga).

### Cohesion (LCOM4)

LCOM4 counts how many unconnected groups a class's methods fall into (1 is cohesive; higher
means the class does more than one job). It is defined only for classes with at least two methods
besides `__init__`. The weeks 1–3 code is functional (classes there are Pydantic and ORM models
with no behaviour), so nothing qualifies.

| Week | Classes measured | Mean LCOM4 | Classes with LCOM4 > 1 |
| --- | --- | --- | --- |
| 1–3 | 0 | n/a | 0 |
| 4 | 3 | 1.00 | 0 |
| 5 | 11 | 1.09 | 1 |
| 6 | 19 | 1.84 | 5 |

Week 6 is the first design with behavioural classes in quantity (bus, consumers, relay, reply
waiters), and 5 of its 19 split into more than one group. Combined with the CC and MI findings,
the week 6 messaging code (`bus.py`) is where cohesion is weakest.

### Limits of these metrics

- Import-level coupling is a proxy. For weeks 5–6 the real coupling is at the wire, so the table
  above is reported alongside it.
- LCOM4 on 3–19 classes is a small sample, and it says nothing about weeks 1–3.
- SLOC counts Python only. nginx configuration, compose files, JSON schemas and YAML (a
  significant part of week 6) are not counted, so week 6 is understated.
- Week 3 is measured at its final streaming state, week 4 at 006, week 6 at 009. The 009 tuning
  added the scan fast path, so week 6's size includes some optimization code; the 008 snapshot
  was not measured separately.
- Static metrics describe structure, not run-time behaviour, and were not weighted by how much each
  component is used.

## 3. Architecture characteristics (FoSA-style chart)

Star ratings (1–5) are the author's judgement from the evidence in §1 (measured), §2 (metrics)
and the drills named below. Rows marked "measured" follow the numbers directly. The others are
judgement. Weeks are rated at their optimized state.

**Expected pattern.** The chart follows the shape FoSA predicts as a system is split into
independently deployable parts: the characteristics that come from *separation* rise, and the
ones that come from *being one thing* fall. Fault tolerance, modularity, deployability, elasticity
and scalability rise, because failure domains, data and deployment units are separated. Week 4
still has one shared PostgreSQL, so it remains a single point of failure, whereas week 5 gives
each service its own store and week 6 removes direct peer calls (the independence, crash-recovery
and broker-outage drills; coupling and shared-code share in §2). Per-service WORKERS and pool
settings, and six partitions on the completions topic, give the later designs levers the
monolith lacks. Simplicity, single-machine performance and whole-system testability fall, because
separation adds moving parts and hops: SLOC grows 4.7×, from 19 files to 72, from 1 to 5 deployable
parts plus a broker (§2), and default tx/s falls from 328 to 180 while the COMPLETE mean rises
from 2.4 to 19.5 ms (§1). Testing each service alone gets easier, but testing the system needs a
compose stack and a broker (failure suite, real-broker tests and chaos drills), so the
whole-system rating drops. Data consistency also weakens, from single-transaction guarantees to a
saga (week 5) and eventual consistency with an outbox (week 6). The one place the benchmark
cannot confirm the rise is scalability, since every run is on a single host (§1). That rating is
architectural, supported by the stress ÷ default completion-attempts-per-second ratio (computed
from the §1 reports; to be added as a §1 table) rising from about 1.2 to 1.5, which shows the
later designs gain more from added concurrency.

| Characteristic | 1 Monolith | 2 Layered | 3 Pipelined | 4 Service-based | 5 Microservices | 6 Event-driven |
| --- | --- | --- | --- | --- | --- | --- |
| Deployability | ★ | ★ | ★ | ★★★ | ★★★★ | ★★★★ |
| Elasticity | ★ | ★ | ★ | ★★★ | ★★★★ | ★★★★★ |
| Scalability (architectural capacity) | ★★ | ★★ | ★★ | ★★★ | ★★★★ | ★★★★★ |
| Availability / fault tolerance | ★ | ★ | ★★ | ★★ | ★★★★ | ★★★★★ |
| Performance (latency, §1) | ★★★★★ | ★★★★★ | ★★★★★ | ★★★ | ★★★ | ★★ |
| Simplicity | ★★★★★ | ★★★★ | ★★★ | ★★★ | ★★ | ★ |
| Testability (whole system) | ★★ | ★★★★ | ★★★★ | ★★★ | ★★★ | ★★ |
| Modularity / evolvability (coupling, §2) | ★★ | ★★★ | ★★★★ | ★★★ | ★★★★★ | ★★★★ |
| Maintainability (CC, MI, LCOM4, size; §2) | ★★★★ | ★★★★★ | ★★★★ | ★★★★ | ★★★ | ★★ |
| Data consistency | ★★★★★ | ★★★★★ | ★★★★★ | ★★★★★ | ★★★ (saga) | ★★★ (eventual, outbox) |
| Observability | ★★★ | ★★★ | ★★★ | ★★ | ★★ | ★ |

Evidence to cite for the non-performance columns (all already in the repo):
- **Availability / fault tolerance:** `check_independence.sh`, `check_crash_recovery.sh`,
  `check_broker_outage.sh` (spec 008 README, week 6: each service survives a peer being down; outage drill
  found and fixed a real producer-wedge defect). Weeks 1–4 have no equivalent because a single
  process or one shared database is a single point of failure.
- **Data consistency:** the invariant (`tools/verify_invariant.py`) is a single-transaction
  guarantee up to week 4, a saga in week 5, and eventual with outbox and idempotent consumers
  in week 6.
- **Modularity changes from the first draft, from the metrics:** week 1 ★ → ★★ (it already has a
  stable core and 12 components, I ≈ 0.4); week 4 ★★★★ → ★★★ (all four services import a
  shared data layer holding 49 % of the code); week 6 ★★★★★ → ★★★★ (no peer addresses, but a
  shared messaging library is 25 % of the code and 12 event schemas are the contract).
- **Maintainability** is new: week 2 scores best (MI 86.9, worst function CC 11). The score falls
  with size and with stateful messaging classes (week 6: MI min 17.8, 5 of 19 classes with
  LCOM4 > 1).
- **Scalability caveat:** the measured throughput fell as the architecture became more
  distributed. The reason is a closed-loop client and a hot-row lock on one host (§1 items 2–4),
  not the style's potential on separate machines. The write-up must say that the benchmark
  cannot show the horizontal scaling the later designs are for.

## 4. Trade-off discussion

### What each step bought and what it cost

| Step | Gained | Measured cost (§1, §2) |
| --- | --- | --- |
| 1 → 2 monolith → layered | Better MI (81 → 87), split the 360-SLOC `services` module | None measurable (741 vs 733 tx/s, same-session A/B) |
| 2 → 3 layered → pipelined | Analytics isolated and streaming (004) | None at run time (stress p95 74 → 64 ms after 004); +25 % SLOC |
| 3 → 4 pipelined → service-based | Independent deployment of four services | −31 % / −29 % tx/s (mostly Docker, not the style), 49 % of code in a shared data layer |
| 4 → 5 service-based → microservices | Private stores, shared data layer removed (49 % → 11 %) | 76 % / 87 % of week 4 tx/s; saga turns 1 transaction into 3 plus a hop; +71 % SLOC |
| 5 → 6 microservices → event-driven | No peer addresses; survives broker and peer outages (drills) | 90 % / 86 % of week 5 tx/s after 009; shared messaging library 3.7×, +62 % SLOC, worst MI and LCOM |

### Notes

1. Where performance went and why: platform (Docker), then lock-hold time, then saga hops, then
   broker queueing. The first is environment cost. The other three are architecture-inherent.
2. The metrics line up with the intended boundaries. Coupling moved rather than disappeared:
   shared database (week 4) → HTTP contracts (week 5) → topics, schemas and a shared messaging
   library (week 6). Cohesion and maintainability get worse as behavioural classes appear in
   week 6 (§2).
3. When each style is the right choice for this workload (one store, one hot SKU, 100 stations
   on one host: the monolith wins. A multi-store chain with independent failure domains:
   events win).
4. Threats to validity: single host, closed-loop client, hot-SKU depletion, Docker Desktop
   overhead, run-to-run noise (week 5's discarded first pair, week 4's battery note on 006), no
   repeated trials, no confidence intervals.

## 5. Reproducing this document

Performance table: `git log --all --diff-filter=A -- 'load-client/reports/**/*.json'` and read
each JSON's `transactionsPerSecond`, `itemsPerSecond` and per-operation latencies.

Metrics: export each week's source with `git archive <ref> backend/app` (weeks 1–3) or
`backend/services backend/packages backend/gateway backend/events` (weeks 4–6), refs as in §2,
then run

```
pip install radon lizard
python -I backend/scripts/analysis/analyze.py <export-dir> app   # weeks 1-3
python -I backend/scripts/analysis/analyze.py <export-dir> svc   # weeks 4-6
```

The script prints JSON (size, CC, MI, per-component Ce/Ca/I, LCOM4). Wire-level coupling was
counted by reading each service's `config.py` for peer URLs and `events/topics.yaml` for topics.
