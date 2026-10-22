"""The persistent analytics pipeline against Checkout's scan log (spec FR-012, FR-012a, FR-012b).

Scans go in through the Checkout Service only; nothing calls Analytics. Every window the
pipeline publishes must equal a direct SQL count over the same bounds in Checkout's database,
and the pipeline must never be re-created per slide.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from sqlalchemy import text

from analytics_service import state
from analytics_service.supervisor import PipelineSupervisor
from checkout_service.db import SessionLocal

_ORACLE = text(
    "SELECT sku, count(*) AS scan_count FROM transaction_item"
    " WHERE id > :s AND id <= :e GROUP BY sku ORDER BY count(*) DESC, sku LIMIT 200"
)


@pytest.fixture
def client(checkout_client):
    return checkout_client


async def oracle(start: int, end: int) -> list[dict]:
    async with SessionLocal() as s:
        rows = (await s.execute(_ORACLE, {"s": start, "e": end})).all()
    return [{"sku": r.sku, "scanCount": r.scan_count} for r in rows]


async def snapshot_end() -> int:
    return state.latest.window_end if state.latest is not None else 0


async def wait_for(pred, timeout=10.0):
    async def loop():
        while not await pred():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), timeout)


async def start_supervisor(**kw) -> tuple[PipelineSupervisor, asyncio.Task]:
    sup = PipelineSupervisor(**kw)
    task = asyncio.create_task(sup.run())

    async def started():
        return sup.instances == 1

    await wait_for(started, 5)
    return sup, task


async def scan(client, tx: str, sku: str) -> None:
    r = await client.post(f"/transactions/{tx}/items", json={"sku": sku})
    assert r.status_code == 200, r.text


async def open_tx(client) -> str:
    r = await client.post("/transactions", json={"stationId": "pipeline"})
    return r.json()["transactionId"]


async def scan_concurrently(client, total: int, catalog_size: int, workers: int = 8) -> None:
    rng = random.Random(42)
    skus = [f"SKU-{rng.randint(1, min(catalog_size, 12)):06d}" for _ in range(total)]
    txs = [await open_tx(client) for _ in range(workers)]

    async def worker(w: int):
        for sku in skus[w::workers]:
            await scan(client, txs[w], sku)

    await asyncio.gather(*(worker(w) for w in range(workers)))


async def test_published_windows_match_sql_under_concurrent_scans(client, catalog_size):
    published: list = []
    sup, task = await start_supervisor(on_publish=published.append)
    try:
        await scan_concurrently(client, 1600, catalog_size)

        async def reached_1500():
            return await snapshot_end() == 1500

        await wait_for(reached_1500)

        ends = [r.window_end for r in published]
        assert ends and ends == sorted(set(ends)) and ends[-1] == 1500
        assert all(e % 500 == 0 for e in ends)
        for r in published:
            assert r.ranking == await oracle(r.window_start, r.window_end), r.window_end
        assert sup.instances == 1  # one pipeline for the whole run (FR-012a)

        body = (await client.get("/analytics/popular-items?limit=200")).json()
        assert (body["windowStart"], body["windowEnd"]) == (500, 1500)
        assert [{"sku": i["sku"], "scanCount": i["scanCount"]} for i in body["items"]] == await oracle(500, 1500)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_every_window_is_published_when_scans_arrive_in_slides(client):
    published: list = []
    sup, task = await start_supervisor(on_publish=published.append)
    try:
        tx = await open_tx(client)
        for n, end in enumerate((500, 1000, 1500)):
            for i in range(500):
                await scan(client, tx, f"SKU-{(i * (n + 1)) % 9 + 1:06d}")

            async def published_end():
                return bool(published) and published[-1].window_end == end

            await wait_for(published_end)

        assert [r.window_end for r in published] == [500, 1000, 1500]
        assert [r.window_start for r in published] == [0, 0, 500]
        for r in published:
            assert r.ranking == await oracle(r.window_start, r.window_end)
        assert sup.instances == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
