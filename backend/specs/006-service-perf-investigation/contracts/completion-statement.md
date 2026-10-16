# Contract: `repos.checkout.complete_basket`

Internal contract between `checkout_service.service.complete_transaction` and
`checkout_data.repos.checkout` (owner: Checkout). The external HTTP contract
(`spec/` OpenAPI, error catalog) is **unchanged** by spec 006.

## Signature

```python
async def complete_basket(
    session: AsyncSession,      # inside the caller's session.begin(); tx row already locked
    tx_id: int,
    skus: list[str],            # basket SKUs, ascending, unique
    qtys: list[int],            # qtys[i] is the quantity of skus[i], each ≥ 1
    total_cents: int,           # Σ price × qty from the in-memory catalog
    threshold: int,             # LOW_STOCK_THRESHOLD
) -> Row                        # (ok: bool, completed_at: datetime | None, short: list[str] | None)
```

## Preconditions (the caller guarantees them)

- It is called inside an explicit transaction (`session.begin()`), after `lock_transaction`
  returned an `OPEN` row for `tx_id` and `get_basket` returned a non-empty basket.
- `skus` is sorted ascending with no duplicates, and `len(skus) == len(qtys) ≥ 1`.

## Behaviour: one SQL statement, one round trip

1. **Lock** `inventory_stock` rows for `skus`, in ascending SKU order (`FOR UPDATE`), before
   any modification.
2. **Check**: `ok` ⇔ every SKU in `skus` has a stock row and `current_stock ≥ qty`.
3. If `ok`:
   - **decrement** every row by its `qty`
   - **insert** one `low_stock_alert(sku, current_stock, threshold)` per SKU where
     `current_stock < threshold ≤ current_stock + qty` (post-decrement values)
   - **finalize** the transaction: `status = 'COMPLETED'`, `completed_at = now()`,
     `total_amount_cents = total_cents`
4. If not `ok`: **modify nothing**.

## Result

| Field | `ok = true` | `ok = false` |
|---|---|---|
| `ok` | `true` | `false` |
| `completed_at` | the transaction's new `completed_at` | `NULL` |
| `short` | `NULL` | the SKUs that are missing or have `current_stock < qty`, ascending |

## Caller obligations

- **`ok = false`**: raise `ApiError(INSUFFICIENT_STOCK, "Insufficient stock for " + ", ".join(short) + ".")`.
  The `session.begin()` block rolls back and releases the locks. This is the same message as 005.
- **`ok = true`**: return the receipt. The `session.begin()` block commits. That `COMMIT` is
  the only client round trip after the stock locks are granted.

## Verified by

- Existing contract/integration tests (unchanged expectations):
  - `tests/contract/test_transactions_complete.py`
  - `tests/integration/test_concurrent_completion.py`
  - `tests/integration/test_low_stock.py`
  - `tests/integration/test_checkout_flow.py`
- A new integration test for the all-or-nothing case: a basket with one short SKU leaves every
  stock row, alert table and the transaction untouched, and `short` lists exactly the
  short SKUs.
- `verify_invariant` and `pg_stat_database.deadlocks = 0` after every load run
  ([quickstart](../quickstart.md) V4–V5).
