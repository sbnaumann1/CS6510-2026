"""Retrying or duplicating a completion never applies it twice (SC-004, FR-013)."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient
from sqlalchemy import text

from checkout_service import repo
from checkout_service.db import SessionLocal as CheckoutSessionLocal
from tests.failure._helpers import (
    adjustment_states,
    assert_consistent,
    basket,
    complete,
    completion_state,
    status_of,
    stock_of,
    tx_number,
)

HOT = "SKU-000001"


async def test_ten_retries_leave_the_state_of_completing_once(client):
    tx = await basket(client, {HOT: 3})

    first = await complete(client, tx)
    repeats = [await complete(client, tx) for _ in range(9)]

    assert first.status_code == 200
    assert [r.status_code for r in repeats] == [409] * 9
    assert {r.json()["error"] for r in repeats} == {"TRANSACTION_NOT_OPEN"}
    assert await stock_of(HOT) == 97
    assert await adjustment_states(tx) == [(1, "APPLIED")]
    await assert_consistent()


async def test_simultaneous_duplicates_produce_one_completion(client, mesh):
    tx = await basket(client, {HOT: 2})
    app = client._transport.app  # the same gateway twin, so the calls genuinely overlap
    clients = [AsyncClient(app=app, base_url="http://test") for _ in range(5)]
    try:
        results = await asyncio.gather(*(complete(c, tx) for c in clients))
    finally:
        for c in clients:
            await c.aclose()

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409, 409, 409]
    assert await stock_of(HOT) == 98, "stock reduced exactly once"
    await assert_consistent()


async def test_a_late_reserve_after_a_release_of_the_same_attempt_is_refused(client, mesh):
    tx = await basket(client, {HOT: 1})
    async with AsyncClient(transport=mesh, base_url="http://inventory:8000") as inv:
        released = await inv.delete(f"/internal/reservations/{tx}", params={"epoch": 1})
        assert released.status_code == 200 and released.json()["unitsRestored"] == 0
        late = await inv.post(
            "/internal/reservations",
            json={"transactionId": tx, "epoch": 1, "lines": [{"sku": HOT, "quantity": 1}]},
        )
        # A different attempt is not blocked by it.
        fresh = await inv.post(
            "/internal/reservations",
            json={"transactionId": tx, "epoch": 2, "lines": [{"sku": HOT, "quantity": 1}]},
        )
    assert late.status_code == 409 and late.json()["error"] == "RESERVATION_RELEASED"
    assert fresh.status_code == 200
    assert await stock_of(HOT) == 99


async def test_a_retry_takes_over_a_saga_whose_driver_died(client):
    tx = await basket(client, {HOT: 2})
    n = tx_number(tx)
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            await repo.mark_completing(c, n, 170)  # the driver died right here
            await c.execute(
                text("UPDATE completion SET updated_at = now() - interval '1 minute'")
            )

    r = await complete(client, tx)  # the lease has expired: this call takes it over

    assert r.status_code == 200
    assert await status_of(client, tx) == "COMPLETED"
    assert await completion_state(tx) == (1, "COMPLETED")
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_a_retry_while_a_live_driver_is_working_is_refused(client):
    tx = await basket(client, {HOT: 2})
    n = tx_number(tx)
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            await repo.mark_completing(c, n, 170)  # fresh lease: someone is on it

    r = await complete(client, tx)

    assert r.status_code == 409 and r.json()["error"] == "TRANSACTION_NOT_OPEN"
    assert "completing" in r.json()["message"]
    assert await stock_of(HOT) == 100


async def test_scanning_a_completing_transaction_is_refused(client):
    tx = await basket(client, {HOT: 1})
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            await repo.mark_completing(c, tx_number(tx), 85)
    r = await client.post(f"/transactions/{tx}/items", json={"sku": HOT})
    assert r.status_code == 409 and r.json()["error"] == "TRANSACTION_NOT_OPEN"
