# Quickstart & Validation Guide

**Feature**: `002-layered-architecture` — how to verify the refactor preserves behavior while
introducing the four layers. Design rationale in [research.md](./research.md); the new internal
contract in [contracts/layer-boundaries.md](./contracts/layer-boundaries.md); the
database-access-layer function signatures in [data-model.md](./data-model.md).

This is a structural refactor with no new setup: everything provisioned for
`001-checkout-backend` (PostgreSQL, the seeded catalog, the Java load client) is reused as-is —
see `specs/001-checkout-backend/quickstart.md` for first-time provisioning.

## 1. Regression: the existing test suite, unmodified

```bash
cd backend
uv run pytest
```

Expected: every test that passed before this refactor still passes (101 at the time of the
refactor) with no assertion changed. The only test edit is the import path in
`tests/integration/test_popular_items.py` (spec FR-009, SC-001; research R5 correction). This is the primary oracle; everything below is a supplementary check specific
to *this* feature (the previous suite already proves *checkout behavior* didn't change).

## 2. Structural checks: the layer-boundary contract holds

Run the three greps from
[contracts/layer-boundaries.md](./contracts/layer-boundaries.md#verifying-the-contract-holds).
Each should produce no `FAIL` line:

```bash
grep -rn "text(" app/transactions app/analytics app/api
grep -rn "from app.transactions\|import app.transactions" app/analytics
grep -rn "from app.analytics\|import app.analytics" app/transactions
grep -rn "\.commit()\|\.rollback()\|session\.begin(" app/db
```

This is SC-002 and SC-003 made runnable.

## 3. Confirm `app/services/` and `app/background.py` are gone

```bash
test ! -d app/services && test ! -f app/background.py && echo OK
```

Per plan.md's Structure Decision, these are deleted, not kept as shims — nothing outside
`backend/app/` imports them.

## 4. Performance: no regression on the hot paths (SC-004)

Re-run the same load-client scenario 001 measured against, and compare against the baseline table
in `specs/001-checkout-backend/plan.md`:

```bash
cd ../load-client
./run.sh --stations 10 --duration 60   # exact invocation per load-client/README.md
```

Expected: `START_TRANSACTION` / `SCAN_ITEM` / `COMPLETE_TRANSACTION` p50/p95/p99 stay within the
ranges already recorded for the monolith (p50 ≤ ~1 ms for start/scan, ≤ ~2.5 ms for complete; see
plan.md's Technical Context for the exact figures). A small increase from added function-call
indirection is expected to be noise-level; a large one (multiples, not percent) would indicate an
accidental extra round trip introduced during extraction (e.g. a repo function opening its own
connection instead of using the passed session) and should be treated as a bug, not accepted as
"the cost of layering."

## 5. Manual smoke check (optional, if not trusting steps 1–4 alone)

```bash
uv run python scripts/seed.py --reset
uv run uvicorn app.main:app --port 8080 &
curl -s localhost:8080/items | head -c 200
curl -s -X POST localhost:8080/transactions -d '{"stationId":"s1"}' | python3 -m json.tool
curl -s localhost:8080/inventory/low-stock | python3 -m json.tool
curl -s localhost:8080/analytics/popular-items | python3 -m json.tool
```

Expected: identical response shapes to pre-refactor (compare against
`specs/001-checkout-backend/contracts/openapi.yaml` if in doubt).
