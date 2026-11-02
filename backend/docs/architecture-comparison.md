# Architecture comparison: the self-checkout system, weeks 1–9

> **Status: DRAFT.** Sections 1 and 4 are filled from committed evidence. Section 2 (code metrics)
> is waiting for the analysis run, deferred so it does not disturb load tests on this host.
> Sections 3 and 5 are provisional and will be re-checked against the metrics.

## 0. The implementations

| Wk | Spec | Architecture | Style (FoSA) | Source |
| --- | --- | --- | --- | --- |
| 1 | 001 | Single FastAPI app, PostgreSQL | Monolith | branch `001-checkout-backend` |
| 2 | 002 | Same app refactored into four layers | Layered monolith | `refactor/002-layered-architecture` |
| 3 | 003 | Pipelined analytics (4-stage filter pipeline) | Pipeline inside the monolith | `feature/pipelined-analytics`, merge `6a92f7b` |
| 4 | 004 | Streaming analytics | Pipeline / streaming | merge `7155890` |
| 5 | 005 | Four domain services behind an nginx gateway, one shared PostgreSQL | Service-based | `005-service-architecture` |
| 6 | 006 | Same, hot-path latency fixes (not a new architecture) | Service-based | `006-hot-path-latency` |
| 7 | 007 | Private store per service, saga between Checkout and Inventory over HTTP | Microservices | `007-microservices-architecture` |
| 8 | 008 | Same services, events over Kafka, outbox | Event-driven microservices | merge `ff7b112` |
| 9 | 009 | Kafka tuning (profiling, consumer pipelining) | Event-driven microservices | `009-kafka-optimization` (uncommitted work) |

## 1. Performance evidence

All numbers are from the committed load-client JSON reports (summary extracted by script from git
history). Same load client, default config is 10 stations / 60 s, stress is 100 stations / 120 s,
stock 10 000 per SKU unless marked "clean" (2 000 000 per SKU, nothing depletes).

**Reading the numbers.** With stock 10 000 the hot SKUs sell out and later completions are
refused with `409 INSUFFICIENT_STOCK`. That is correct behaviour (the invariant is the graded
check) but it inflates tx/s because refusals are cheap. Compare like with like: depleting runs
with depleting runs, clean runs with clean runs. Absolute numbers also drift with host state, for
example the week 6 note about battery power.

### Depleting runs (stock 10 000), the common basis

| Wk | Architecture | Default tx/s | Stress tx/s | Stress COMPLETE p95 | Report |
| --- | --- | --- | --- | --- | --- |
| 1 | Monolith | 328.4 | 276.2 | 74.6 ms | `20260918-170238`, `-170702` |
| 3 | Pipelined analytics | 336.9 (see note) | 271.8 | 73.9 ms | `20260929-202454`, `-202733` |
| 4 | Streaming (native processes) | 320.9 | 269.1 | 63.9 ms | `20261002-163856`, `-164059` |
| 5 | Service-based (Docker) | 220.6 | 190.9 | 225.0 ms | `005-…/20261013-093301`, `-093519` |
| 6 | + hot-path fixes | 262.5 | 224.5 | 121.6 ms | `006-…/20261016-082652`, `-082910` |
| 7 | Microservices, private stores | 200.5 | 196.2 | 175.7 ms | `007-…/20261023-104702`, `-104920` |
| 8 | Event-driven (Kafka) | 166.2 | 160.2 | 303.1 ms | `008-…/20261027-220244`, `-220513` |
| 9 | Kafka tuning | not yet measured | | | |

### Clean runs (stock 2 000 000), monolith family only

| Wk | Default tx/s | Stress tx/s | Stress COMPLETE p95 |
| --- | --- | --- | --- |
| 1 (V7 clean) | 788.0 | not run | |
| 2 (layered) | 637.5 | 832.4 | 143.5 ms |
| 2 A/B (pre-refactor vs layered, 09-23) | 741 vs 733 (per reports README) | n/a | n/a |
| ? `20260928-173337/-173540` (clean, purpose unconfirmed) | 731.7 | 838.7 | 142.0 ms |

The week 2 refactor cost no measurable throughput. The A/B in
`backend/specs/002-layered-architecture/reports/` puts it within noise.

### Reading across the weeks

1. **Monolith family (weeks 1–4): about 270–335 tx/s, p95 COMPLETE about 64–75 ms.** Layering and
   pipelining are essentially free at run time.
2. **Week 5, going to services in Docker: −31 % default, −29 % stress.** The week 6 investigation
   (`specs/006-service-perf-investigation/investigation.md`) attributes it as about 44 % Docker
   port forwarding, about 52 % VM overhead, about 0 % from nginx or the service code. It is
   largely the deployment platform, not the architectural style. The same 005 code natively
   served 827 tx/s against the monolith's 818 in a same-session check.
3. **Week 6** recovers +19 % / +18 % by shortening the hot-SKU lock hold to one statement. The
   lesson is that in a closed-loop client, per-hop latency under a hot row lock multiplies.
4. **Week 7, private stores and a saga:** 91 % / 103 % of week 5, 76 % / 87 % of week 6. The cost
   is three local transactions plus an HTTP hop where week 6 had one transaction.
5. **Week 8, Kafka:** 83 % / 82 % of week 7. COMPLETE mean doubles (10.2 → 21.6 ms default,
   80 → 183 ms stress). Inventory's own work is about 10–20 ms. The rest of the ~150 ms wait is
   consumer queueing plus two broker hops.
6. **Correctness held in every run.** The stock invariant passed on every graded run, including
   the cross-store version from week 7 on.

> Open items to verify before submitting: the label for `20261002-092147` (91.3 tx/s, 82.7 %
> COMPLETE errors) looks like a bad run and `006df17` re-ran it ("clean seeding"); decide which of
> the 0929 / 1002-0940 / 1002-1524 reports belong to week 3 vs week 4 (the README only
> labels `1002-163856/164059` as "Week 4 baseline"). Week 9 reports will be added after the
> profiling work is committed.

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

| Wk | Files | SLOC | Classes | Funcs | Mean CC | Max CC | MI (mean) | Mean I | LCOM4 (mean) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | – | – | – | – | – | – | – | – | – |
| 2 | – | – | – | – | – | – | – | – | – |
| 3 | – | – | – | – | – | – | – | – | – |
| 4 | – | – | – | – | – | – | – | – | – |
| 5 | – | – | – | – | – | – | – | – | – |
| 7 | – | – | – | – | – | – | – | – | – |
| 8 | – | – | – | – | – | – | – | – | – |
| 9 | – | – | – | – | – | – | – | – | – |

(Week 6 is a tuning of 5 and week 9 of 8. Include only if the metrics differ meaningfully.)

## 3. Architecture characteristics (FoSA-style chart), PROVISIONAL

Star ratings (1–5) are the author's judgement from the evidence cited, to be revised after
section 2. Each rating needs a justification in the final text.

| Characteristic | Monolith (1) | Layered (2) | Pipeline (3/4) | Service-based (5/6) | Microservices (7) | Event-driven (8/9) |
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
  `check_broker_outage.sh` (week 8 README: each service survives a peer being down; outage drill
  found and fixed a real producer-wedge defect). Weeks 1–5 have no equivalent because a single
  process or one shared database is a single point of failure.
- **Data consistency:** the invariant (`tools/verify_invariant.py`) is a single-transaction
  guarantee up to week 6, a saga from week 7, and eventual with outbox and idempotent consumers
  in week 8.
- **Scalability caveat:** the measured throughput fell as the architecture became more
  distributed. The reason is a closed-loop client and a hot-row lock on one host (§1 item 2–5),
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
   overhead, run-to-run noise (week 7's discarded first pair, week 6's battery note), no
   repeated trials, no confidence intervals.

## 5. Reproducing this document

Performance table: `git log --all --diff-filter=A -- 'load-client/reports/**/*.json'` and read
each JSON's `transactionsPerSecond`, `itemsPerSecond` and per-operation latencies. Metrics: see
§2 Method. The scripts live in `backend/scripts/analysis/` (to be added).
