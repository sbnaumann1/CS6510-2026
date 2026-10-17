# Data Model: Hot-Path Latency Reduction (spec 006)

**No schema changes.** The tables, columns, indexes and ownership registry
(`checkout_data.ownership.TABLES`) are exactly those of 001/005. This feature changes *how*
Checkout writes them, not what they hold.

## 1. Completion: state transition and side effects

Same transition as 001: `transaction.status` `OPEN → COMPLETED`, all-or-nothing. What changes is
the number of client round trips, and therefore how long the locks are held:

| Step | Today (005) | Spec 006 | Locks held while the client waits |
|---|---|---|---|
| `BEGIN` | 1 round trip | 1 round trip | — |
| 1. lock `transaction` row `FOR UPDATE`; check `OPEN` | 1 | 1 | the tx row |
| 2. basket `(sku, qty)` in SKU order | 1 | 1 | the tx row |
| 3. lock stock rows in SKU order | 1 | **one statement: `complete_basket`** ([contract](./contracts/completion-statement.md)) | — |
| 4. conditional decrement | 1 | (in that statement) | tx row + all basket stock rows |
| 5. alert rows for threshold crossings | 0–1 | (in that statement) | tx row + all basket stock rows |
| 6. finalize `transaction` | 1 | (in that statement) | tx row + all basket stock rows |
| `COMMIT` | 1 | 1 | tx row + all basket stock rows |
| **Round trips after the stock locks are granted** | **4–5** | **1** | |

**Invariants**: unchanged and verified exactly as before.

- **INV stock** (graded): for every SKU, `initial − current_stock == Σ qty of COMPLETED line
  items`, and `current_stock ≥ 0` (`verify_invariant`).
- **All-or-nothing**: if any SKU is short, no `inventory_stock`, `low_stock_alert` or
  `transaction` row is modified. The transaction stays `OPEN`.
- **Deadlock freedom**: every completion acquires stock locks in ascending SKU order before
  modifying any row. The plan shape that guarantees this is in research R1.
- **Alert crossing**: an alert is inserted iff `current_stock < threshold ≤ current_stock + qty`
  after the decrement, in the same transaction (001 SC-005, FR-006).
- **No double completion**: the transaction row lock (step 1) serializes concurrent completes
  of the same transaction. The second one sees status ≠ `OPEN`.

## 2. Session modes

| Factory / dependency (`checkout_data`) | Engine | Transaction framing | Used by |
|---|---|---|---|
| `SessionLocal` / `get_session` | `engine` | explicit `BEGIN … COMMIT` | Checkout `complete`, `GET /transactions/{id}`, sweeper; Catalog, Inventory, Analytics (unchanged) |
| `AutocommitSessionLocal` / `get_autocommit_session` *(new)* | `autocommit_engine` = `engine.execution_options(isolation_level="AUTOCOMMIT")`, **same pool** | none: each statement commits on its own | Checkout `POST /transactions` (start), `POST /transactions/{id}/items` (scan) |

Rules:

- Only routes whose database work is **one atomic statement** may use the autocommit session.
  Error-classification reads that follow a failed statement (scan's `get_status`) are allowed:
  they are reads, and the failed statement modified nothing.
- Connections return to the pool with the default isolation level. Mixing both factories on
  one pool is safe (research R2 probe).
- Repository functions still never begin, commit or roll back (002 R4).

## 3. Configuration

| Name | Where | Default | Meaning |
|---|---|---|---|
| `UVICORN_HTTP` *(new)* | each service image (`ENV` in its Dockerfile) | `httptools` | uvicorn `--http` implementation. Set to `h11` for any deployment where a client that sends `Upgrade: h2c` reaches the service directly ([service-runtime contract](./contracts/service-runtime.md)) |
| `LOW_STOCK_THRESHOLD`, `DB_POOL_SIZE`, `WORKERS`, … | unchanged | unchanged | see 005 data-model §4 |

## 4. `checkout-data` repository surface (`repos/checkout.py`), 0.5.0 → 0.6.0

| Function | Status |
|---|---|
| `insert_transaction`, `scan_item`, `get_status`, `lock_transaction`, `get_basket`, `get_transaction`, `sweep_abandoned` | unchanged |
| `complete_basket(session, tx_id, skus, qtys, total_cents, threshold) → Row(ok, completed_at, short)` | **new** |
| `lock_stock`, `decrement_stock`, `insert_alerts`, `complete_transaction` | **removed**: replaced by `complete_basket` |

`checkout_service/alerts.py` (`emit_crossings`) is removed with them. The crossing rule now lives
in the `alerts` CTE, and the contract documents it.
