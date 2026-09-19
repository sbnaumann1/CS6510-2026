"""Integration test: concurrency correctness (T023).

This is the core the exercise is built around — INV-3, the last-unit race, INV-6.
"""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import create_app


def _clients(n: int):
    """n independent clients over the same app, so requests genuinely overlap."""
    app = create_app()
    return [
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        for _ in range(n)
    ]


async def test_inv3_concurrent_double_complete_decrements_once(client, session):
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    for _ in range(3):
        await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000010"})

    a, b = _clients(2)
    try:
        r1, r2 = await asyncio.gather(
            a.post(f"/transactions/{tx}/complete", json={}),
            b.post(f"/transactions/{tx}/complete", json={}),
            return_exceptions=False,
        )
    finally:
        await a.aclose()
        await b.aclose()

    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], codes

    stock = await session.scalar(
        text("SELECT current_stock FROM inventory_stock WHERE sku = 'SKU-000010'")
    )
    assert stock == 97, "stock must be decremented exactly once"


async def test_last_unit_race_has_exactly_one_winner(client, session):
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 1 WHERE sku = 'SKU-000011'")
    )
    await session.commit()

    clients = _clients(2)
    try:
        txs = []
        for c in clients:
            tx = (await c.post("/transactions", json={"stationId": "s"})).json()[
                "transactionId"
            ]
            await c.post(f"/transactions/{tx}/items", json={"sku": "SKU-000011"})
            txs.append(tx)

        results = await asyncio.gather(
            *[c.post(f"/transactions/{tx}/complete", json={}) for c, tx in zip(clients, txs)]
        )
    finally:
        for c in clients:
            await c.aclose()

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], codes

    stock = await session.scalar(
        text("SELECT current_stock FROM inventory_stock WHERE sku = 'SKU-000011'")
    )
    assert stock == 0, "never negative, never oversold"


async def test_concurrent_baskets_never_oversell(client, session):
    """Ten stations race for eight units; exactly eight are sold."""
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 8 WHERE sku = 'SKU-000012'")
    )
    await session.commit()

    clients = _clients(10)
    try:
        txs = []
        for c in clients:
            tx = (await c.post("/transactions", json={"stationId": "s"})).json()[
                "transactionId"
            ]
            await c.post(f"/transactions/{tx}/items", json={"sku": "SKU-000012"})
            txs.append(tx)

        results = await asyncio.gather(
            *[c.post(f"/transactions/{tx}/complete", json={}) for c, tx in zip(clients, txs)]
        )
    finally:
        for c in clients:
            await c.aclose()

    ok = sum(1 for r in results if r.status_code == 200)
    conflict = sum(1 for r in results if r.status_code == 409)
    assert ok == 8
    assert conflict == 2

    stock = await session.scalar(
        text("SELECT current_stock FROM inventory_stock WHERE sku = 'SKU-000012'")
    )
    assert stock == 0


async def test_overlapping_baskets_do_not_deadlock(client, session):
    """Reverse-ordered baskets: SKU-ordered locking makes deadlock structurally
    impossible (research R10 step 3)."""
    clients = _clients(8)
    try:
        txs = []
        for i, c in enumerate(clients):
            tx = (await c.post("/transactions", json={"stationId": "s"})).json()[
                "transactionId"
            ]
            skus = ["SKU-000013", "SKU-000014", "SKU-000015"]
            if i % 2:
                skus.reverse()
            for sku in skus:
                await c.post(f"/transactions/{tx}/items", json={"sku": sku})
            txs.append(tx)

        results = await asyncio.gather(
            *[c.post(f"/transactions/{tx}/complete", json={}) for c, tx in zip(clients, txs)]
        )
    finally:
        for c in clients:
            await c.aclose()

    assert all(r.status_code == 200 for r in results)

    stock = dict(
        (
            await session.execute(
                text(
                    "SELECT sku, current_stock FROM inventory_stock"
                    " WHERE sku IN ('SKU-000013','SKU-000014','SKU-000015')"
                )
            )
        ).all()
    )
    assert set(stock.values()) == {92}


async def test_inv6_open_and_cancelled_transactions_decrement_nothing(client, session):
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    for _ in range(5):
        await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000016"})

    # Still OPEN.
    assert (
        await session.scalar(
            text("SELECT current_stock FROM inventory_stock WHERE sku = 'SKU-000016'")
        )
        == 100
    )

    await session.execute(
        text("UPDATE transaction SET status = 'CANCELLED' WHERE id = :id"),
        {"id": int(tx.removeprefix("tx-"))},
    )
    await session.commit()

    assert (
        await session.scalar(
            text("SELECT current_stock FROM inventory_stock WHERE sku = 'SKU-000016'")
        )
        == 100
    )
