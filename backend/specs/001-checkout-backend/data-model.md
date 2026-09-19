# Phase 1 Data Model: Self-Checkout Backend API

**Feature**: `001-checkout-backend` | **Date**: 2026-09-16 | **Source**: [spec.md](./spec.md) Key Entities, resolved by [research.md](./research.md)

SQLAlchemy ORM models in `app/models.py` are the schema source of truth; the DDL below is what
they must produce. All money is integer cents (R4); all timestamps are `TIMESTAMPTZ`.

## Entity overview

| Entity | Table | Lifecycle | Row count at 60 s / 10 stations |
| --- | --- | --- | --- |
| CatalogItem | `catalog_item` | Immutable after seed | 2 000 |
| InventoryStock | `inventory_stock` | Mutated only at completion | 2 000 |
| Transaction | `transaction` | OPEN → COMPLETED / CANCELLED | 10⁴–10⁵ |
| TransactionItem | `transaction_item` | Insert-only, immutable | 10⁵–10⁶ |
| LowStockAlert | `low_stock_alert` | Insert-only, immutable | ≤ 2 000 per run (one per crossing) |
| PopularItemWindow | `popular_window_snapshot` | Single row, replaced every 500 scans | 1 |

## catalog_item

```sql
CREATE TABLE catalog_item (
  sku         TEXT PRIMARY KEY,           -- 'SKU-000001' … 'SKU-002000'
  name        TEXT    NOT NULL,           -- 'Item 1' … (mock's formula, R12)
  price_cents INTEGER NOT NULL CHECK (price_cents > 0)
);
```

- Seeded once from the mock's deterministic formulas: `sku = 'SKU-%06d' % i`,
  `name = 'Item %d' % i`, `price = round(0.5 + (i % 47) * 0.35, 2)` → `price_cents`.
- Immutable during a run. Loaded into `app/catalog_cache.py` at startup as
  `dict[sku] -> (name, price_cents)`, plus a pre-rendered `/items` JSON body (R7).
- **`GET /items` must return rows in ascending SKU order** — the load client's Zipf rank is
  positional, so response order decides which SKUs are hot (R12).

## inventory_stock

```sql
CREATE TABLE inventory_stock (
  sku           TEXT PRIMARY KEY REFERENCES catalog_item(sku),
  current_stock INTEGER NOT NULL CHECK (current_stock >= 0),
  initial_stock INTEGER NOT NULL
);
```

- `initial_stock` is set at seed time and never changes; it is the left-hand side of the graded
  invariant and what `scripts/verify_invariant.py` reads.
- The `CHECK (current_stock >= 0)` is a backstop, not the mechanism: the conditional
  `UPDATE ... WHERE current_stock >= qty` (R10) is what actually prevents negative stock. If the
  check ever fires, a concurrency bug has been introduced and the request fails loudly.
- No `version` column — locking is pessimistic (row locks in SKU order), per the spec's
  concurrency assumption.

## transaction

```sql
CREATE TYPE tx_status AS ENUM ('OPEN', 'COMPLETED', 'CANCELLED');

CREATE TABLE transaction (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  station_id          TEXT      NOT NULL,
  status              tx_status NOT NULL DEFAULT 'OPEN',
  item_count          INTEGER   NOT NULL DEFAULT 0,
  running_total_cents BIGINT    NOT NULL DEFAULT 0,
  total_amount_cents  BIGINT,                      -- set at completion
  started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at        TIMESTAMPTZ
);

CREATE INDEX transaction_open_started_idx ON transaction (started_at) WHERE status = 'OPEN';
```

- Public `transactionId` = `'tx-' || id` (R5). Inbound IDs not matching `tx-<digits>` are
  rejected as `404 TRANSACTION_NOT_FOUND` without a query.
- `item_count` / `running_total_cents` are denormalized counters maintained by the scan CTE (R9)
  so `ScanResult` needs no aggregation.
- Partial index supports the abandonment sweeper (`OPEN` older than `TX_ABANDON_MINUTES`) without
  indexing the millions of completed rows.

### State transitions

```
            POST /transactions
                    │
                    ▼
                 OPEN ──── POST /{id}/complete (stock available) ──▶ COMPLETED   [terminal]
                  │  │
                  │  └──── sweeper: started_at < now() - 5 min ─────▶ CANCELLED  [terminal]
                  │
                  └─────── POST /{id}/complete (insufficient stock) ─▶ stays OPEN, 409
```

| From | Event | To | Response |
| --- | --- | --- | --- |
| — | `POST /transactions` | OPEN | 201 `Transaction` |
| OPEN | `POST /{id}/items` | OPEN | 200 `ScanResult` |
| OPEN | `POST /{id}/complete`, all stock available | COMPLETED | 200 `Receipt` |
| OPEN | `POST /{id}/complete`, basket empty | OPEN | 409 `EMPTY_BASKET` |
| OPEN | `POST /{id}/complete`, any SKU short | OPEN | 409 `INSUFFICIENT_STOCK` |
| OPEN | sweeper timeout | CANCELLED | n/a — no stock is decremented |
| COMPLETED / CANCELLED | any scan or complete | unchanged | 409 `TRANSACTION_NOT_OPEN` |

An insufficient-stock completion deliberately leaves the transaction OPEN: the rollback undoes
nothing else, and a retry after restock would succeed. The sweeper eventually cancels it.

## transaction_item

```sql
CREATE TABLE transaction_item (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- global scan sequence (R6)
  transaction_id   BIGINT  NOT NULL REFERENCES transaction(id),
  sku              TEXT    NOT NULL REFERENCES catalog_item(sku),
  unit_price_cents INTEGER NOT NULL,
  scanned_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX transaction_item_tx_idx  ON transaction_item (transaction_id);
CREATE INDEX transaction_item_sku_idx ON transaction_item (sku);   -- invariant verification
```

- **One row per scanned unit** — the contract defines one scan call as exactly one physical unit.
  Receipt lines are produced by `GROUP BY sku` at completion, not stored.
- `unit_price_cents` is captured at scan time so a receipt is reproducible even if the catalog
  were ever to change (it does not, during a run).
- `id` doubles as the global scan sequence feeding the popular-items window; `windowStart` /
  `windowEnd` in the API response are values of this column. Gaps and small out-of-order
  allocations across workers are expected and documented (R6).
- `transaction_item_sku_idx` exists for `verify_invariant.py`'s per-SKU aggregate, not for the
  hot path. It costs insert throughput; drop it and verify from a sequential scan if measurement
  shows it matters.

## low_stock_alert

```sql
CREATE TABLE low_stock_alert (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sku           TEXT    NOT NULL REFERENCES catalog_item(sku),
  current_stock INTEGER NOT NULL,
  threshold     INTEGER NOT NULL,
  triggered_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX low_stock_alert_sku_time_idx ON low_stock_alert (sku, triggered_at DESC);
```

- Immutable historical record, written inside the completion transaction **only on a crossing**
  (`current_stock < threshold <= current_stock + qty`), so one row per SKU per descent rather than
  one per completion. This is what keeps SC-005 true — the alert row is committed atomically with
  the decrement that caused it, so no query can observe a below-threshold item without its alert.
- Crossings are detected against the server's configured default threshold
  (`LOW_STOCK_THRESHOLD`, default 50). A `?threshold=` query override is a *view* over live
  stock, not a re-evaluation of history (see below).

### `GET /inventory/low-stock` semantics

The response is computed from live stock, joined to the most recent alert row for each SKU:

```sql
SELECT c.sku, c.name, s.current_stock, :threshold AS threshold,
       COALESCE(a.triggered_at, now()) AS triggered_at
  FROM inventory_stock s
  JOIN catalog_item c USING (sku)
  LEFT JOIN LATERAL (
        SELECT triggered_at FROM low_stock_alert
         WHERE sku = s.sku ORDER BY triggered_at DESC LIMIT 1
  ) a ON TRUE
 WHERE s.current_stock < :threshold
 ORDER BY a.triggered_at NULLS LAST, c.sku;
```

- `threshold` in the response echoes the effective threshold (query override or configured
  default); `generatedAt` is `now()`.
- Ordering is by alert timestamp then SKU, satisfying FR-007's "timestamp order" while staying
  deterministic for items that have no alert row (possible only when `?threshold=` is raised
  above the configured default).
- With the spec's Zipf workload this list can legitimately grow to hundreds of SKUs late in a
  run; the mock's reports show the same. No pagination — the contract has none.

## popular_window_snapshot

```sql
CREATE TABLE popular_window_snapshot (
  id            SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- single row
  window_size   INTEGER     NOT NULL,
  slide_interval INTEGER    NOT NULL,
  window_start  BIGINT      NOT NULL,   -- transaction_item.id exclusive lower bound
  window_end    BIGINT      NOT NULL,   -- transaction_item.id inclusive upper bound
  computed_at   TIMESTAMPTZ NOT NULL,
  ranking       JSONB       NOT NULL    -- [{"sku": "...", "scanCount": 123}, ...] rank = index+1
);
```

- Replaced wholesale (`INSERT ... ON CONFLICT (id) DO UPDATE`) by the recompute task, which holds
  a `pg_try_advisory_lock` so only one worker recomputes per slide (R6).
- `ranking` stores the top 200 SKUs, which covers any sane `?limit=`; names and `rank` are
  attached at read time from the in-memory catalog cache, so the stored blob stays small.
- Read path: one PK fetch, slice to `limit`. If no row exists yet (fewer than `windowSize` scans
  into the run), the endpoint computes the ranking on demand over whatever scans exist and
  returns them — this is the spec's "fewer than N items in window" edge case, and the reason
  `windowStart` can be 0.

## Cross-entity invariants

| ID | Invariant | Enforced by | Checked by |
| --- | --- | --- | --- |
| INV-1 | `current_stock >= 0` for every SKU | conditional `UPDATE` (R10) + `CHECK` backstop | `verify_invariant.py`, DB constraint |
| INV-2 | `initial_stock - current_stock == COUNT(transaction_item rows in COMPLETED transactions)` per SKU | single-transaction decrement + SKU-ordered locks | `verify_invariant.py` (SC-003, SC-004) |
| INV-3 | A transaction is decremented at most once | `SELECT ... FOR UPDATE` on the transaction row before any stock work | integration test: concurrent double-complete |
| INV-4 | `transaction.item_count == COUNT(transaction_item)` and `running_total_cents == SUM(unit_price_cents)` | scan CTE updates both atomically | integration test after a scripted basket |
| INV-5 | Receipt `totalAmount` == Σ(line `unitPrice` × `quantity`) | integer cents, single conversion at the JSON boundary | contract test |
| INV-6 | No stock is decremented for OPEN or CANCELLED transactions | decrement only in the completion path | `verify_invariant.py` (it counts COMPLETED only) |

## Configuration (env, `app/config.py`)

| Setting | Default | Used by |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg:///checkout?host=/tmp` | app (Unix socket, R3) |
| `CATALOG_SIZE` | 2000 | seed |
| `STOCK_PER_ITEM` | 10000 | seed → `initial_stock` (see research R13) |
| `LOW_STOCK_THRESHOLD` | 50 | alert crossings, default query threshold |
| `POPULAR_WINDOW_SIZE` | 1000 | window recompute |
| `POPULAR_SLIDE_INTERVAL` | 500 | window recompute trigger |
| `TX_ABANDON_MINUTES` | 5 | sweeper |
| `DB_POOL_SIZE` | 12 | per worker; keep `workers × pool < max_connections` |
| `WORKERS` | 4 | `scripts/run_server.sh` |
