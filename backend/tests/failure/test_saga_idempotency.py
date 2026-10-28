"""Retrying or duplicating a completion, or any event of it, never applies it twice
(SC-004, SC-006, FR-011, FR-013)."""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from checkout_service import repo, stock_events
from checkout_service.db import SessionLocal as CheckoutSessionLocal
from checkout_web import events as ev
from tests.failure._helpers import (
    adjustment_states,
    assert_consistent,
    basket,
    complete,
    completion_state,
    set_stock,
    status_of,
    stock_of,
    tx_number,
    wait_until,
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


async def test_simultaneous_duplicates_produce_one_completion(client, stack):
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


async def test_a_late_reserve_after_a_release_of_the_same_attempt_is_refused(client, stack):
    tx = await basket(client, {HOT: 1})

    async def ask(event):
        await stack.bus.publish(ev.CHECKOUT_COMPLETIONS, event, key=tx)

    def replies():
        return stack.bus.events(ev.INVENTORY_STOCK)

    await ask(stock_events.release_request(tx, 1))
    await wait_until(lambda: len(replies()) == 1, what="the release outcome")
    assert replies()[0].type == ev.STOCK_RELEASED and replies()[0].data["unitsRestored"] == 0

    await ask(stock_events.reservation_request(tx, 1, [(HOT, 1)]))  # the same attempt, too late
    await wait_until(lambda: len(replies()) == 2, what="the late reserve's outcome")
    assert replies()[1].type == ev.RESERVATION_REFUSED

    await ask(stock_events.reservation_request(tx, 2, [(HOT, 1)]))  # a different attempt is not blocked
    await wait_until(lambda: len(replies()) == 3, what="the fresh reserve's outcome")
    assert replies()[2].type == ev.STOCK_RESERVED
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


# ---- SC-006: every event delivered 2 times, and 10 times, ends in the same state ------------


async def run_workload(client, session) -> dict:
    """Six baskets: five that complete, one that is refused for stock. Returns the final state."""
    await set_stock(session, "SKU-000009", 1)
    txs = [
        await basket(client, {HOT: 2, "SKU-000002": 1}),
        await basket(client, {"SKU-000003": 3}),
        await basket(client, {"SKU-000004": 1, "SKU-000005": 1}),
        await basket(client, {"SKU-000009": 2}),  # refused: only one unit
        await basket(client, {"SKU-000006": 2}),
        await basket(client, {"SKU-000007": 1}),
    ]
    codes = [(await complete(client, tx)).status_code for tx in txs]
    stock = {sku: await stock_of(sku) for sku in (HOT, "SKU-000002", "SKU-000003", "SKU-000004",
                                                    "SKU-000005", "SKU-000006", "SKU-000007", "SKU-000009")}
    adjustments = [await adjustment_states(tx) for tx in txs]
    await assert_consistent()
    return {"codes": codes, "stock": stock, "adjustments": adjustments}


@pytest.mark.parametrize("extra_copies", [1, 9], ids=["each event twice", "each event ten times"])
async def test_redelivering_every_event_gives_the_same_final_state(client, stack, session, extra_copies):
    """Compare a run with every event delivered `1 + extra_copies` times to the expected
    state of a run with none duplicated (the numbers below are what one delivery yields)."""
    stack.bus.duplicate(lambda topic, event, key: True, copies=extra_copies)

    final = await run_workload(client, session)

    assert final["codes"] == [200, 200, 200, 409, 200, 200]
    assert final["stock"] == {
        HOT: 98, "SKU-000002": 99, "SKU-000003": 97, "SKU-000004": 99, "SKU-000005": 99,
        "SKU-000006": 98, "SKU-000007": 99, "SKU-000009": 1,
    }
    assert final["adjustments"] == [
        [(1, "APPLIED")], [(1, "APPLIED")], [(1, "APPLIED")], [(1, "REJECTED")],
        [(1, "APPLIED")], [(1, "APPLIED")],
    ]


async def test_redelivered_scan_events_are_counted_once_by_analytics(client, stack, analytics, poll_analytics):
    stack.bus.duplicate(lambda topic, event, key: event.type == ev.SCAN_RECORDED, copies=4)
    await basket(client, {HOT: 4, "SKU-000002": 1})

    await poll_analytics()

    body = (await client.get("/analytics/popular-items")).json()
    assert {i["sku"]: i["scanCount"] for i in body["items"]} == {HOT: 4, "SKU-000002": 1}
