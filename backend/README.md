# Self-Checkout Backend — Monolith (FastAPI + PostgreSQL)

Week 1 of the architecture series. Implements the frozen contract in
[`spec.yaml`](./spec.yaml) as a single FastAPI service over one PostgreSQL
instance, measured by the unmodified Java load client in `../load-client/`.

Design artifacts live in [`specs/001-checkout-backend/`](./specs/001-checkout-backend/):
[spec](./specs/001-checkout-backend/spec.md) ·
[plan](./specs/001-checkout-backend/plan.md) ·
[research](./specs/001-checkout-backend/research.md) ·
[data model](./specs/001-checkout-backend/data-model.md) ·
[quickstart](./specs/001-checkout-backend/quickstart.md)

## Prerequisites

- Python 3.11 + [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL 17 (`brew install postgresql@17`), tuned per research R11
- JDK 21+ for the load client

## Setup

```bash
brew services start postgresql@17
createdb checkout

uv sync
cp .env.example .env                       # edit DATABASE_URL if not on the Unix socket
uv run python scripts/seed.py --reset      # schema + 2000 items @ 10000 units each
```

`--reset` is idempotent and **must be re-run before every measured load run** — a
run started against depleted stock is not comparable to anything.

## Run

```bash
./scripts/run_server.sh                    # uvicorn, 4 workers, uvloop, :8080
curl -s localhost:8080/items | head -c 120
```

## Test

```bash
uv run pytest -q                           # contract + integration + unit
uv run pytest -q tests/contract            # error-catalog conformance only
uv run python scripts/verify_invariant.py  # the graded correctness check
```

The suite runs against its own database, `checkout_test`, created automatically on
first run — never the `checkout` database the server uses. That isolation matters:
`seed.py --reset` does `DROP SCHEMA public CASCADE`, so a shared database would let
a test run destroy a live server's schema mid-flight. (The server would keep serving
its cached catalog while every write returned `500`.) Tests and a load run can now
proceed at the same time without touching each other. Override with
`TEST_DATABASE_URL`; the name must end in `_test` or the suite refuses to start.

**`verify_invariant.py` reads the `checkout` database, so run it after a real
workload, not after `pytest`.** On an empty database it reports
`Units decremented: 0` and passes vacuously — green, but proving nothing. A
meaningful run looks like this:

```bash
./run.sh                                             # fresh 2000-item catalog
cd ../load-client && ./run.sh --stations=10 --duration=60
cd ../backend && uv run python scripts/verify_invariant.py
# SKUs checked: 2000 / Units decremented: 97430 / Completed line items: 97430 / PASS
```

## Measure

```bash
uv run python scripts/seed.py --reset
cd ../load-client && ./build.sh && ./run.sh --baseUrl=http://localhost:8080 --stations=10 --duration=60
cd ../backend && uv run python scripts/verify_invariant.py
```

Stress run is the same client with `--stations=100 --duration=120`.

## Inspect stock

`DATABASE_URL` is in asyncpg form, which `psql` will not parse — connect by database
name instead, adding `-h /tmp` if your socket is elsewhere. Everything below reads
`inventory_stock`, the same rows the completion path decrements, so it is the truth
rather than the API's view of it.

```bash
psql -d checkout
```

Start with one row for the whole catalog. `units_sold` here should equal the
`Units decremented` that `verify_invariant.py` reports; `below_threshold` counts
against `LOW_STOCK_THRESHOLD` (default 50):

```sql
SELECT count(*)                                   AS skus,
       sum(initial_stock)                         AS initial_units,
       sum(current_stock)                         AS units_left,
       sum(initial_stock - current_stock)         AS units_sold,
       count(*) FILTER (WHERE current_stock = 0)  AS sold_out,
       count(*) FILTER (WHERE current_stock < 50) AS below_threshold
  FROM inventory_stock;
```

The most depleted SKUs. The client's Zipf sampler keys on catalog position, so a
healthy run puts `SKU-000001` on top and decays steeply from there — if the ranking
looks flat, the load did not run the way you think it did:

```sql
SELECT s.sku, c.name,
       s.initial_stock - s.current_stock AS sold,
       s.current_stock
  FROM inventory_stock s JOIN catalog_item c USING (sku)
 ORDER BY sold DESC, s.sku
 LIMIT 20;
```

A single item — SKUs are zero-padded to six digits, so it is `SKU-000001`, not
`SKU-1`:

```sql
SELECT s.sku, c.name, c.price_cents, s.current_stock, s.initial_stock
  FROM inventory_stock s JOIN catalog_item c USING (sku)
 WHERE s.sku = 'SKU-000001';
```

Alert history. One row per threshold crossing, written inside the completion
transaction, so repeated rows for a SKU mean it crossed, was restocked, and crossed
again — not that the alert fired twice for one descent:

```sql
SELECT sku, current_stock, threshold, triggered_at
  FROM low_stock_alert
 ORDER BY triggered_at DESC
 LIMIT 20;
```

What is hot right now against what is left to sell — the popular-items snapshot
joined to live stock. A high-rank SKU sitting at `0` is what turns later baskets
into `409 INSUFFICIENT_STOCK`:

```sql
SELECT r.ord AS rank,
       r.item->>'sku' AS sku,
       (r.item->>'scanCount')::int AS scans,
       s.current_stock
  FROM popular_window_snapshot p,
       LATERAL jsonb_array_elements(p.ranking) WITH ORDINALITY AS r(item, ord)
  JOIN inventory_stock s ON s.sku = r.item->>'sku'
 ORDER BY r.ord
 LIMIT 20;
```

Transaction mix, for context on the numbers above. Only `COMPLETED` rows moved
stock; a large `OPEN` count after a finished run means baskets were abandoned and
the sweeper has not reached them yet:

```sql
SELECT status, count(*) FROM transaction GROUP BY status ORDER BY status;
```

To watch depletion while a run is in flight, follow any query with `\watch 2` to
re-run it every two seconds. For scripts, use the non-interactive form:

```bash
psql -d checkout -tAc "SELECT sum(initial_stock - current_stock) FROM inventory_stock;"
```

## How it works

| Concern | Approach |
| --- | --- |
| Catalog | Loaded once into memory; `GET /items` is a pre-rendered JSON body in ascending SKU order (the client's Zipf rank is positional) |
| Scan | One CTE: bump the transaction's denormalized counters, insert the line, `RETURNING` both — one round trip |
| Completion | One transaction: lock the tx row, collapse the basket, lock stock rows **in SKU order**, then one conditional batched `UPDATE ... WHERE current_stock >= qty`. A short row count rolls everything back as `409 INSUFFICIENT_STOCK` |
| Never oversell | The conditional `UPDATE` is the mechanism; the `CHECK (current_stock >= 0)` is a backstop that should never fire |
| No deadlocks | Every completion takes stock locks in the same SKU order, so a lock cycle cannot form |
| Low-stock alerts | Written inside the completion transaction, only on a threshold crossing — one row per descent, committed atomically with the decrement |
| Popular items | `transaction_item.id` is the global scan sequence; a hopping window is recomputed out of band every 500 scans into one snapshot row, so all workers agree |
| Money | Integer cents end to end; converted to the contract's `number` once, at the JSON boundary |

## Expected results

A default run depletes the hottest SKU partway through: the client's Zipf sampler
gives rank-1 about 12% of all scans, so its 10 000 units run out after roughly
82 000 scans. Every later basket containing it correctly returns `409`
(see [research R13](./specs/001-checkout-backend/research.md)). That is the system
working, not a fault — `verify_invariant.py` is the pass/fail criterion. A
companion run with `STOCK_PER_ITEM=200000` demonstrates a clean 0% error rate
without clamping stock.

## Layout

```
app/
  main.py            app factory, lifespan, sweeper loop
  config.py          env-backed settings
  db.py              async engine, session factory, advisory lock
  models.py          ORM models = schema source of truth
  money.py           integer-cent helpers + the catalog price formula
  catalog_cache.py   in-memory catalog + pre-rendered /items body
  errors.py          ApiError codes and handlers (400/404/409/500)
  background.py      window recompute trigger, abandoned-tx sweeper
  api/               catalog, transactions, inventory, analytics routes
  services/          transactions (scan CTE, completion), inventory, analytics
scripts/
  seed.py            deterministic reset
  verify_invariant.py  the graded correctness check
  run_server.sh
tests/               contract / integration / unit
```
