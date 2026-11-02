# Architecture comparison: the self-checkout system, six architectures over six weeks

> **Status: DRAFT.** Sections 0 and 1 are filled from committed evidence. Section 2 (code metrics)
> is waiting for the analysis run, deferred so it does not disturb load tests on this host.
> Sections 3 and 5 are provisional and will be re-checked against the metrics.

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
| 6 | Event-driven microservices: Kafka, outbox, idempotent consumers | 008 | 009 Kafka optimization (in progress, uncommitted) | merge `ff7b112`, `009-kafka-optimization` |

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
| 6 | Event-driven | 009 optimized | not yet measured | | | |

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
   wait is consumer queueing plus two broker hops. Spec 009 tunes exactly this.
5. **Correctness held in every run.** The stock invariant passed on every graded run, including
   the cross-store version from week 5 on.

> Open items to verify before submitting: the label for `20261002-092147` (91.3 tx/s, 82.7 %
> COMPLETE errors) looks like a bad run and `006df17` re-ran it ("clean seeding"); confirm that
> `1002-163856/164059` is the 004 streaming result (the README calls it the "Week 4 baseline" for
> spec 005), and decide where `1002-092147/092724/094043/094600/152414/152618` belong (clean
> re-runs and intermediate 003/004 runs). Week 6's 009 optimization reports will be added after
> the profiling work is committed.

## 2. Code metrics (TO BE GENERATED)

### Method

Each version is checked out into its own `git worktree` under a scratch directory (read-only,
no builds). Tools run in a throwaway venv, one at a time at lowest priority (`nice -n 19`).
Scope is production code only: tests, `__pycache__`, and generated files excluded.

| Metric | Tool | Why it matters here |
| --- | --- | --- |
| LoC (SLOC, logical), files, classes, functions | `radon raw`, `lizard` | Size baseline |
| Cyclomatic complexity (mean, max, count of C+ blocks) | `radon cc`, `lizard` | Local complexity |
| Maintainability index | `radon mi` | Aggregate maintainability |
| Afferent / efferent coupling (Ca, Ce), instability I = Ce/(Ca+Ce), per package | custom AST script over imports; cross-checked with `pydeps` | **Coupling** |
| Inter-service coupling: HTTP/Kafka call sites to other services, shared-package imports | grep of clients/topics; `scripts/check_no_peer_calls.sh` result | Coupling at the architecture level |
| Cohesion: LCOM4 per class (connected components of methods sharing attributes) and module-level cohesion (share of intra-package imports) | custom AST script | **Cohesion** |
| Layer / boundary violations | `import-linter` contracts; existing `scripts/check_boundaries.py` | Does the structure hold? |
| Shared code across services | LoC in `packages/` vs `services/` | Hidden coupling |

Caveat to state in the write-up: Python module-level coupling is a proxy. For weeks 5–9 the
real coupling is at the wire level (HTTP contracts, topic schemas), so both views are reported.

### Results

| Week | Architecture | Files | SLOC | Classes | Funcs | Mean CC | Max CC | MI (mean) | Mean I | LCOM4 (mean) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Monolith | – | – | – | – | – | – | – | – | – |
| 2 | Layered | – | – | – | – | – | – | – | – | – |
| 3 | Pipelined | – | – | – | – | – | – | – | – | – |
| 4 | Service-based | – | – | – | – | – | – | – | – | – |
| 5 | Microservices | – | – | – | – | – | – | – | – | – |
| 6 | Event-driven | – | – | – | – | – | – | – | – | – |

Measure each week at its final optimized state (004 for week 3, 006 for week 4, 009 for week 6
once committed). Add the pre-optimization commit as an extra row only if the metrics differ
meaningfully.

## 3. Architecture characteristics (FoSA-style chart), PROVISIONAL

Star ratings (1–5) are the author's judgement from the evidence cited, to be revised after
section 2. Each rating needs a justification in the final text.

| Characteristic | 1 Monolith | 2 Layered | 3 Pipelined | 4 Service-based | 5 Microservices | 6 Event-driven |
| --- | --- | --- | --- | --- | --- | --- |
| Deployability | ★ | ★ | ★ | ★★★ | ★★★★ | ★★★★ |
| Elasticity | ★ | ★ | ★ | ★★★ | ★★★★ | ★★★★★ |
| Scalability (measured, §1) | ★★★★ | ★★★★ | ★★★★ | ★★ | ★★ | ★★ (★★★★ by design) |
| Availability / fault tolerance | ★ | ★ | ★★ | ★★ | ★★★★ | ★★★★★ |
| Performance (latency, §1) | ★★★★★ | ★★★★★ | ★★★★★ | ★★★ | ★★★ | ★★ |
| Simplicity | ★★★★★ | ★★★★ | ★★★ | ★★★ | ★★ | ★ |
| Testability | ★★ | ★★★★ | ★★★★ | ★★★ | ★★★★ | ★★★ |
| Modularity / evolvability | ★ | ★★★ | ★★★★ | ★★★★ | ★★★★★ | ★★★★★ |
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
- **Scalability caveat:** the measured throughput fell as the architecture became more
  distributed. The reason is a closed-loop client and a hot-row lock on one host (§1 items 2–4),
  not the style's potential on separate machines. The write-up must say that the benchmark
  cannot show the horizontal scaling the later designs are for.

## 4. Trade-off discussion (outline)

1. What each step bought and what it cost, as a table of (change, gained, measured cost).
2. Where performance went and why: platform (Docker), then lock-hold time, then saga hops,
   then broker queueing. Distinguish architecture-inherent from environment cost.
3. Coupling and cohesion: how the metrics in §2 line up with the intended boundaries.
4. When each style is the right choice for this workload (one store, one hot SKU, 100 stations
   on one host: the monolith wins. A multi-store chain with independent failure domains:
   events win).
5. Threats to validity: single host, closed-loop client, hot-SKU depletion, Docker Desktop
   overhead, run-to-run noise (week 5's discarded first pair, week 4's battery note on 006), no
   repeated trials, no confidence intervals.

## 5. Reproducing this document

Performance table: `git log --all --diff-filter=A -- 'load-client/reports/**/*.json'` and read
each JSON's `transactionsPerSecond`, `itemsPerSecond` and per-operation latencies. Metrics: see
§2 Method. The scripts live in `backend/scripts/analysis/` (to be added).
