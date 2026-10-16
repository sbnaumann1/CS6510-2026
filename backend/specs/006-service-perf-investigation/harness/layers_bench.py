"""Per-layer cost of one scan, single connection, native PG over the Unix socket.

Run with the worktree venv:  DATABASE_URL=postgresql+asyncpg:///checkout_perf?host=/tmp \
    .venv/bin/python layers_bench.py
Needs a seeded checkout_perf DB (native_exp.sh leaves one behind).
"""
import asyncio, os, statistics, time

import asyncpg

N = 3000
DSN = "postgresql:///checkout_perf?host=/tmp"
SCAN_SQL = """
WITH tx AS (
  UPDATE transaction SET item_count = item_count + 1,
         running_total_cents = running_total_cents + $3
   WHERE id = $1 AND status = 'OPEN'
  RETURNING id, item_count, running_total_cents
), ins AS (
  INSERT INTO transaction_item (transaction_id, sku, unit_price_cents)
  SELECT id, $2, $3 FROM tx RETURNING id
)
SELECT tx.item_count, tx.running_total_cents, (SELECT id FROM ins) AS scan_seq FROM tx
"""


def report(name, ts):
    ts = sorted(ts)
    print(f"{name:42} mean={statistics.fmean(ts)*1000:.3f} ms  p50={ts[len(ts)//2]*1000:.3f}  p95={ts[int(len(ts)*.95)]*1000:.3f}")


async def new_tx(conn):
    return await conn.fetchval("INSERT INTO transaction (station_id) VALUES ('bench') RETURNING id")


async def main():
    conn = await asyncpg.connect(DSN)
    sku, price = await conn.fetchrow("SELECT sku, price_cents FROM catalog_item ORDER BY sku LIMIT 1")

    ts = []
    for _ in range(N):
        t = time.perf_counter(); await conn.fetchval("SELECT 1"); ts.append(time.perf_counter() - t)
    report("asyncpg SELECT 1 (bare round trip)", ts)

    tx = await new_tx(conn); stmt = await conn.prepare(SCAN_SQL); ts = []
    for _ in range(N):
        t = time.perf_counter(); await stmt.fetchrow(tx, sku, price); ts.append(time.perf_counter() - t)
    report("asyncpg scan CTE, autocommit (1 RT)", ts)

    tx = await new_tx(conn); ts = []
    for _ in range(N):
        t = time.perf_counter()
        await conn.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
        await stmt.fetchrow(tx, sku, price)
        await conn.execute("COMMIT")
        ts.append(time.perf_counter() - t)
    report("asyncpg BEGIN + scan + COMMIT (3 RT)", ts)
    await conn.close()

    from checkout_data import SessionLocal, catalog, dispose_engine
    from checkout_data.repos import checkout as repo
    from checkout_service import service as svc

    async with SessionLocal() as s:
        await catalog.load(s)
        txid = (await repo.insert_transaction(s, "bench")).id
        await s.commit()
    ts = []
    for _ in range(N):
        t = time.perf_counter()
        async with SessionLocal() as s:
            await repo.scan_item(s, txid, sku, price)
            await s.commit()
        ts.append(time.perf_counter() - t)
    report("SQLAlchemy session: repo.scan_item+commit", ts)

    async with SessionLocal() as s:
        txid = (await repo.insert_transaction(s, "bench")).id
        await s.commit()
    ts = []
    for _ in range(N):
        t = time.perf_counter()
        async with SessionLocal() as s:
            await svc.scan_item(s, txid, sku)
        ts.append(time.perf_counter() - t)
    report("service layer: svc.scan_item", ts)
    await dispose_engine()


asyncio.run(main())
