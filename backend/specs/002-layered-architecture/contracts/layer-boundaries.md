# Internal Contract: Layer Boundaries

**Feature**: `002-layered-architecture`

The external HTTP contract (`specs/001-checkout-backend/contracts/openapi.yaml` and
`error-catalog.md`) is frozen and unchanged by this feature — see spec.md FR-007. What this
feature introduces is a new **internal** contract: which of the four layers may import which.
This file is that contract.

## Allowed import directions

```text
app/api/*        ──▶  app/transactions/*   (transactions.py router)
app/api/*        ──▶  app/analytics/*      (inventory.py, analytics.py routers)
app/api/*        ──▶  app/catalog_cache.py (catalog.py router — no business layer for this one)
app/api/*        ──▶  app/errors.py, app/schemas.py

app/transactions/*  ──▶  app/db/transactions_repo.py
app/transactions/*  ──▶  app/catalog_cache.py, app/errors.py, app/money.py, app/config.py

app/analytics/*     ──▶  app/db/analytics_repo.py
app/analytics/*     ──▶  app/catalog_cache.py, app/errors.py, app/money.py, app/config.py

app/db/*            ──▶  sqlalchemy, app/config.py   (nothing else)
```

## Forbidden

- `app/transactions/*` importing anything from `app/analytics/*`, and vice versa. Neither business
  layer needs the other: Transactions produces the scan sequence and stock state that Analytics
  reports on, but it does so by writing to the database, not by calling into Analytics code.
- `app/api/*` importing SQL, a session, or `app.db.*` directly. Every router goes through a
  business-layer function (or `catalog_cache` for the one read that has no business rule at all).
- `app/db/*` raising an `ApiError`, checking a business condition (e.g. "is this SKU known"), or
  calling `session.commit()` / `.rollback()` / `.begin()`. A repo function's contract is "run this
  query, return what the database returned" — see data-model.md for each function's exact
  signature.
- Two API-layer router modules importing helpers from each other. Today `api/analytics.py`
  imports `positive_int_param` from `api/inventory.py`; this feature moves shared request-parsing
  helpers (`_json_body`, `_required_str`, `positive_int_param`) into `api/_request.py` so routers
  only import sideways from a shared helper module, never from one another.

## Verifying the contract holds

Since this feature's own success criteria (spec SC-002, SC-003) are stated as "verified by
inspection," the checks below are greps, not a new lint tool (research.md R6):

```bash
# No raw SQL outside the database-access layer.
grep -rn "text(" app/transactions app/analytics app/api && echo "FAIL: SQL leaked out of app/db" 

# No cross-import between the two business layers.
grep -rn "from app.transactions\|import app.transactions" app/analytics && echo "FAIL"
grep -rn "from app.analytics\|import app.analytics" app/transactions && echo "FAIL"

# No transaction-boundary calls inside the database-access layer.
grep -rn "\.commit()\|\.rollback()\|session\.begin(" app/db && echo "FAIL"
```

All three should print nothing (i.e. no `FAIL` line) once the refactor is complete.
