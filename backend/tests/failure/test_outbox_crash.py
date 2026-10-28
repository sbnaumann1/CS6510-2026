"""A state change and its event never diverge (FR-012, FR-014; research R7): a crash between
COMMIT and publish leaves the event in the outbox, and the sweeper delivers it."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from checkout_service import outbox as checkout_outbox
from checkout_service import recovery, repo, stock_events
from checkout_service.db import SessionLocal as CheckoutSessionLocal
from checkout_web import events as ev
from inventory_service import outbox as inventory_outbox
from inventory_service.db import SessionLocal as InventorySessionLocal
from tests.failure._helpers import (
    adjustment_states,
    adjustment_states_are,
    assert_consistent,
    basket,
    complete,
    completion_state,
    status_of,
    stock_of,
    tx_number,
    wait_until,
)

HOT = "SKU-000001"


async def unpublished(session_factory, table="outbox") -> int:
    async with session_factory() as s:
        return int(await s.scalar(text(f"SELECT count(*) FROM {table} WHERE published_at IS NULL")))


async def test_a_checkout_crash_between_commit_and_publish_loses_nothing(client, stack):
    """Step 1 committed (state RESERVING and the request in the outbox) and Checkout died
    before publishing. The sweeper sends the request; recovery then ends the completion."""
    tx = await basket(client, {HOT: 2})
    n = tx_number(tx)
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            epoch = await repo.mark_completing(c, n, 170)
            request = stock_events.reservation_request(tx, epoch, [(HOT, 2)])
            await repo.insert_outbox(c, ev.CHECKOUT_COMPLETIONS, tx, request)
    # ...and the process died here: nothing was published.
    assert stock_events.get_client() is not None
    assert stack.bus.records(ev.CHECKOUT_COMPLETIONS) == []
    assert await unpublished(CheckoutSessionLocal) == 1

    assert await checkout_outbox.sweep_once(age_s=0.0) == 1, "the sweeper found it"
    await wait_until(lambda: adjustment_states_are(tx, [(1, "APPLIED")]), what="Inventory applying the request")
    assert await unpublished(CheckoutSessionLocal) == 0
    assert await checkout_outbox.sweep_once(age_s=0.0) == 0, "and does not send it again"

    # Nobody was waiting for the reply, so the completion is stuck RESERVING until recovery.
    assert await completion_state(tx) == (1, "RESERVING")
    assert await recovery.run_once(lease_s=0.0) == 1
    assert await completion_state(tx) == (1, "ABORTED")
    assert await adjustment_states(tx) == [(1, "RELEASED")]
    assert await stock_of(HOT) == 100
    assert await status_of(client, tx) == "OPEN"
    await assert_consistent()


async def test_an_inventory_crash_between_commit_and_publish_is_covered_by_the_sweeper(client, stack, monkeypatch):
    """Inventory committed the stock change and its outcome row, then 'died' before publishing
    (the fast path is skipped). The sweeper publishes the outcome while the shopper waits."""

    async def crash(topic, key, event):
        return False  # the process died before publishing

    monkeypatch.setattr(inventory_outbox, "publish_now", crash)
    tx = await basket(client, {HOT: 2})

    pending = asyncio.create_task(complete(client, tx))
    await wait_until(lambda: unpublished(InventorySessionLocal), what="the outcome in Inventory's outbox")
    assert await stock_of(HOT) == 98, "the stock change committed with it"
    assert await inventory_outbox.sweep_once(age_s=0.0) == 1
    r = await pending

    assert r.status_code == 200
    assert await adjustment_states(tx) == [(1, "APPLIED")]
    assert await unpublished(InventorySessionLocal) == 0
    await assert_consistent()


async def test_an_outcome_lost_with_its_service_is_re_emitted_when_asked_again(client, stack, monkeypatch):
    """Inventory dies before publishing and its outbox is not swept in time. Checkout asks again
    after its first wait; Inventory finds its recorded adjustment and answers from the record."""
    calls = {"n": 0}
    real = inventory_outbox.publish_now

    async def lose_the_first(topic, key, event):
        calls["n"] += 1
        return False if calls["n"] == 1 else await real(topic, key, event)

    monkeypatch.setattr(inventory_outbox, "publish_now", lose_the_first)
    tx = await basket(client, {HOT: 2})

    r = await complete(client, tx)

    assert r.status_code == 200
    assert await stock_of(HOT) == 98, "applied once although the request was handled twice"
    assert [e.data.get("replayed") for e in stack.bus.events(ev.INVENTORY_STOCK)] == [True]
    await assert_consistent()


async def test_published_rows_are_marked_and_old_ones_purged(client, stack):
    assert (await complete(client, await basket(client, {HOT: 1}))).status_code == 200
    assert await unpublished(CheckoutSessionLocal) == 0 and await unpublished(InventorySessionLocal) == 0

    async with CheckoutSessionLocal() as c:
        await c.execute(text("UPDATE outbox SET published_at = now() - interval '2 hours'"))
        await c.commit()
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            assert await repo.delete_published_older_than(c, hours=1) == 1
