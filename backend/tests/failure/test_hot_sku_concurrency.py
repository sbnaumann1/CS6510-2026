"""Many concurrent completions that all want the hot SKU never oversell it, and the stores
agree afterwards (spec 006: SKU-000001 is in about two thirds of baskets). Since spec 008 the
completions race through Kafka: requests and replies interleave, and Inventory handles
different transactions concurrently."""

from __future__ import annotations

import asyncio

from tests.failure._helpers import (
    adjustment_states,
    assert_consistent,
    basket,
    complete,
    set_stock,
    stock_of,
)

HOT = "SKU-000001"


async def test_fifty_baskets_race_for_the_last_units(client, session):
    await set_stock(session, HOT, 60)
    txs = [await basket(client, {HOT: 2, f"SKU-{2 + i % 9:06d}": 1}) for i in range(50)]

    results = await asyncio.wait_for(asyncio.gather(*(complete(client, tx) for tx in txs)), 60)

    won = [r for r in results if r.status_code == 200]
    lost = [r for r in results if r.status_code == 409]
    assert len(won) == 30 and len(lost) == 20, "exactly the units available were sold"
    assert {r.json()["error"] for r in lost} == {"INSUFFICIENT_STOCK"}
    assert await stock_of(HOT) == 0
    await assert_consistent()
    for tx, r in zip(txs, results):
        if r.status_code == 409:
            assert await adjustment_states(tx) == [(1, "REJECTED")], "a refused basket moves no stock"


async def test_overlapping_baskets_do_not_deadlock_through_the_saga(client):
    txs = [
        await basket(client, {"SKU-000001": 1, "SKU-000002": 1} if i % 2 else {"SKU-000002": 1, "SKU-000001": 1})
        for i in range(30)
    ]
    results = await asyncio.wait_for(asyncio.gather(*(complete(client, tx) for tx in txs)), 30)
    assert all(r.status_code == 200 for r in results)
    assert await stock_of(HOT) == 70
    await assert_consistent()
