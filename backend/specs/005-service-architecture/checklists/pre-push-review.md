# Pre-Push Review: Analytics Scan Ingestion

**Purpose**: A required review before the 005 refactor is pushed or merged. Requested 2026-10-13.
**Feature**: [spec.md](../spec.md) · Design: [research.md R5–R6](../research.md)

Don't push or merge the implementation until every box is checked, or its outcome is
written down here.

**Reviewed 2026-10-13** against commit-range `worktree-service-architecture-spec`
(implementation T001–T066). Evidence came from:
- the graded runs (`logs/compose-{default,stress}.log`);
- a dedicated un-graded stress run whose stack was kept up for database checks (100 stations / 120 s, 608 717 scans);
- the test suite (146 passing).

## 1. No scan is ever skipped (FR-012b, research R6)

- [x] **Late commits are never skipped**: a scan whose id commits after a higher id is still ingested. Confirm the TailIngest gap rule (contiguous `last_id` watermark, re-read from `last_id`) is implemented as designed, not as a naive "advance to max id seen".
  - *Code*: `TailIngest.run` re-reads `id > last_id` every poll. It holds rows above a hole in `pending`, and `_drain` only advances `last_id` through contiguous ids (`pipeline.py`).
  - *Tests*: `test_late_commit_inside_grace_is_never_skipped` (ids 4 and 5 wait for 3). The 8-concurrent-scanner integration test has ids committing out of order, and every window matched SQL.
  - *Limit*: production doesn't count holes that later filled. That path is covered by tests only.
- [x] **Gap grace period is sufficient**: check `POPULAR_GAP_GRACE_MS` (1 000 ms) against real behavior. In the stress run logs, count holes declared permanent. Any id declared a gap that later appears in `transaction_item` is a skipped scan, and that must be zero.
  - `gap skipped` lines: 0 in the graded default run, 0 in the graded stress run, 0 in the review stress run.
  - The review run's table has `max(id) − count(*) = 0`, so no sequence value was ever lost.
  - Skipped ids later present in the table: **0** (none were skipped).
  - 1 000 ms was never reached by any hole under 100 stations.
- [x] **Permanent gaps don't stall the pipeline**: a sequence value that never commits is skipped after the grace period, and windows keep publishing.
  - `test_permanent_gap_is_skipped_after_grace` (id 3 skipped, 4 emitted, `gap skipped id=3` logged).
  - `test_consecutive_missing_ids_cost_one_grace_period`: a run of missing ids costs one grace period, not one per id.
  - `test_tail_stuck_behind_a_hole_sleeps_instead_of_spinning`: no busy loop while waiting.
- [x] **Windows match the database exactly**: after the stress run, compare every published window against `window_counts` SQL over the same bounds (the `test_persistent_pipeline.py` oracle). It must have zero mismatches.
  - *Stress run*: the final snapshot `(607500, 608500]` compared against a direct SQL count over the same bounds (sku, count and rank) had **0 mismatches**.
  - *Every window*: the snapshot is one row that each window overwrites, so earlier windows can't be re-checked after the fact. Every-window equality is proven by `test_published_windows_match_sql_under_concurrent_scans` (8 concurrent scanners, every published window equals the oracle) and `test_every_window_is_published_when_scans_arrive_in_slides`.
- [x] **Restart loses nothing**: stop Analytics mid-run, keep scanning, restart it, and confirm the backfill covers every id in the latest window (quickstart V6, step 5).
  - `scripts/check_independence.sh`: 500 scans while Analytics was stopped. After restart it served `windowEnd` equal to the latest boundary (2000, then 3000 on a re-run), and every other route kept serving.
  - `test_restart_backfills_and_publishes_the_latest_window`: the restart logs `bootstrap_from=500`, and its first window `(500, 1500]` equals the oracle.
- [x] **Back-pressure doesn't drop**: confirm the bounded `q1` blocks TailIngest rather than discarding items when downstream filters are slow, and that only OutputFilter's latest-wins slot ever drops anything (superseded *windows*, never scans).
  - *Code review*:
    - `q1`/`q2`/`q3` are `asyncio.Queue(maxsize=…)` and every producer uses `await put()`, which blocks and never discards.
    - `pending` is bounded by one 1 000-row batch, because each poll re-reads from `last_id`.
    - `LatestSlot.put` replaces only an unconsumed `RankedData`. `TERMINATE` is kept separately, so shutdown never drops the final window.
  - *Observed*: the review stress run published 1 098 of ~1 217 windows (superseded ones dropped by design). It finished within one boundary of the head (window end 608 500, max id 608 717).
- [x] **Leader handover is gap-free**: if a second Analytics replica takes the advisory lock, its bootstrap starts at `b₀ − 1000` and the first window it publishes is complete.
  - `test_standby_takes_over_without_losing_scans` (added during this review): a standby runs no pipeline while the leader holds the lock. When the leader goes, it takes over mid-stream, and every window it publishes equals the oracle, through 1500.
  - Bootstrap uses `first_boundary = b₀`, so a partial `b₀ − 500` window can't be published. This was a bug in the original plan, caught by tests during implementation.

## 2. Decide on analytics `LISTEN` (research R5 alternatives)

Currently TailIngest only polls (100 ms idle sleep). Review whether to add PostgreSQL
`LISTEN` as a **wake-up hint only**, with polling staying the source of truth.

- [x] **Measure first**: from the default and stress runs, record the observed scan-to-published-window latency and the idle query rate. Is polling actually a problem?
  - *Scan-to-visible latency*: measured on the idle review stack, 8 trials. Scan the boundary-crossing item via Checkout, then poll Analytics until it serves that window. Results: **min 12 ms, median 54 ms, max 84 ms**. The 2 s target (SC-005) is met about 25× over.
  - *Idle query rate*: ≈ 10 trivial index reads/s (1000 / `POPULAR_POLL_MS`).
  - *Under load*: back-to-back 1 000-row batches. The stress runs stayed within one boundary of the head.
  - **Polling is not a problem.**
- [x] **Coupling check**: `LISTEN` only helps if something sends `NOTIFY`. Choose between Checkout sending it, a trigger on `transaction_item`, or neither. Checkout sending it makes Checkout aware of a consumer, which conflicts with FR-009 and the spec's "no messaging between services". A trigger adds work to the hottest insert.
  - **Neither.** Checkout sending `NOTIFY` makes Checkout aware of Analytics, which is the coupling the assignment forbids. A trigger adds a `pg_notify` to every scan insert, the hottest statement in the system, to save at most one poll interval (≤ 100 ms) on a window clients only read at the end of a run.
- [x] **Failure behavior**: if adopted, missed notifications (Analytics down, connection drop) must not lose scans. Polling must still cover everything.
  - Not adopted, so there's nothing to lose. Polling already covers Analytics downtime: the tail resumes from the table (restart test above).
- [x] **Decision recorded**: adopt or reject, with the reason, as an update to research.md R5 (and FR-009/the boundary rules if adopted).
  - **Rejected.** Recorded in research.md R5, "Alternatives considered". FR-009 and the boundary rules are unchanged.

## Outcome

| Item | Result | Notes |
| --- | --- | --- |
| Scan-skip review | **PASS** | 0 gap skips across ~1.49 M scans in three runs (graded default, graded stress, review stress) and 0 lost ids in the review run's table; final window equals SQL; restart and handover publish only complete, exact windows. One test (leader handover) added during review. Production doesn't count holes that later filled; that path is covered by unit and integration tests. |
| LISTEN decision | **REJECTED** | Polling gives a 54 ms median (84 ms max) scan-to-visible latency against a 2 s target at ~10 queries/s idle. `NOTIFY` would couple Checkout to Analytics or tax the hottest insert, for ≤ 100 ms gain. Research R5 updated. |
