# Pre-Push Review: Analytics Scan Ingestion

**Purpose**: A required review before the 005 refactor is pushed or merged. Requested 2026-10-13.
**Feature**: [spec.md](../spec.md) · Design: [research.md R5–R6](../research.md)

Don't push or merge the implementation until every box is checked, or its outcome is
written down here.

## 1. No scan is ever skipped (FR-012b, research R6)

- [ ] **Late commits are never skipped**: a scan whose id commits after a higher id is still ingested. Confirm the TailIngest gap rule (contiguous `last_id` watermark, re-read from `last_id`) is implemented as designed, not as a naive "advance to max id seen".
- [ ] **Gap grace period is sufficient**: check `POPULAR_GAP_GRACE_MS` (1 000 ms) against real behavior. In the stress run logs, count holes declared permanent. Any id declared a gap that later appears in `transaction_item` is a skipped scan, and that must be zero.
- [ ] **Permanent gaps don't stall the pipeline**: a sequence value that never commits is skipped after the grace period, and windows keep publishing.
- [ ] **Windows match the database exactly**: after the stress run, compare every published window against `window_counts` SQL over the same bounds (the `test_persistent_pipeline.py` oracle). It must have zero mismatches.
- [ ] **Restart loses nothing**: stop Analytics mid-run, keep scanning, restart it, and confirm the backfill covers every id in the latest window (quickstart V6, step 5).
- [ ] **Back-pressure doesn't drop**: confirm the bounded `q1` blocks TailIngest rather than discarding items when downstream filters are slow, and that only OutputFilter's latest-wins slot ever drops anything (superseded *windows*, never scans).
- [ ] **Leader handover is gap-free**: if a second Analytics replica takes the advisory lock, its bootstrap starts at `b₀ − 1000` and the first window it publishes is complete.

## 2. Decide on analytics `LISTEN` (research R5 alternatives)

Currently TailIngest only polls (100 ms idle sleep). Review whether to add PostgreSQL
`LISTEN` as a **wake-up hint only**, with polling staying the source of truth.

- [ ] **Measure first**: from the default and stress runs, record the observed scan-to-published-window latency and the idle query rate. Is polling actually a problem?
- [ ] **Coupling check**: `LISTEN` only helps if something sends `NOTIFY`. Choose between Checkout sending it, a trigger on `transaction_item`, or neither. Checkout sending it makes Checkout aware of a consumer, which conflicts with FR-009 and the spec's "no messaging between services". A trigger adds work to the hottest insert.
- [ ] **Failure behavior**: if adopted, missed notifications (Analytics down, connection drop) must not lose scans. Polling must still cover everything.
- [ ] **Decision recorded**: adopt or reject, with the reason, as an update to research.md R5 (and FR-009/the boundary rules if adopted).

## Outcome

| Item | Result | Notes |
| --- | --- | --- |
| Scan-skip review | _pending_ | |
| LISTEN decision | _pending_ | |
