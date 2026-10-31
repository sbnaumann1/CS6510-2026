"""A poison event is set aside without blocking later events, and an outage is not mistaken
for a poison event (FR-015; research R9)."""

from __future__ import annotations

import pytest

from checkout_web import events as ev
from inventory_service import handlers
from tests.failure._helpers import (
    adjustment_states,
    assert_consistent,
    basket,
    complete,
    stock_of,
    wait_until,
)

HOT = "SKU-000001"


@pytest.fixture
def quick_retries(monkeypatch):
    monkeypatch.setattr(handlers, "BACKOFF_S", 0.005)


async def restart_inventory(stack):
    await stack.stop_service("inventory")
    await stack.start_service("inventory")


async def test_malformed_bytes_are_dead_lettered_and_the_next_request_is_served(client, stack, quick_retries):
    await restart_inventory(stack)
    await stack.bus.publish_raw(ev.CHECKOUT_COMPLETIONS, b"{this is not an event", key="tx-77")

    await wait_until(lambda: stack.bus.records(ev.DEAD_LETTERS), what="the dead letter")
    dead = stack.bus.events(ev.DEAD_LETTERS)[0]
    assert dead.type == ev.DEAD_LETTER
    assert dead.data["sourceTopic"] == ev.CHECKOUT_COMPLETIONS and dead.data["consumer"] == "inventory"
    assert dead.data["attempts"] == 1 and dead.data["event"] == "{this is not an event"

    # Later events on the same topic are not blocked behind it.
    tx = await basket(client, {HOT: 2})
    assert (await complete(client, tx)).status_code == 200
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_a_handler_that_keeps_failing_is_dead_lettered_after_five_tries(client, stack, quick_retries, monkeypatch):
    async def boom(request):
        raise RuntimeError("boom")

    real = handlers.handle_reservation
    monkeypatch.setattr(handlers, "handle_reservation", boom)
    await restart_inventory(stack)
    tx = await basket(client, {HOT: 2})

    r = await complete(client, tx)  # no reply ever comes: a clean failure, then the rollback

    assert r.status_code == 503
    await wait_until(lambda: len(stack.bus.records(ev.DEAD_LETTERS)) >= 1, what="the dead letter")
    dead = stack.bus.events(ev.DEAD_LETTERS)[0].data
    assert dead["attempts"] == 5 and "boom" in dead["error"] and dead["consumer"] == "inventory"
    assert dead["event"]["type"] == ev.STOCK_RESERVATION_REQUESTED

    # The partition was not blocked: with the handler fixed, the next basket completes.
    monkeypatch.setattr(handlers, "handle_reservation", real)
    await restart_inventory(stack)
    tx2 = await basket(client, {HOT: 1})
    assert (await complete(client, tx2)).status_code == 200
    assert await adjustment_states(tx2) == [(1, "APPLIED")]
    await assert_consistent()


async def test_an_unreachable_database_is_waited_out_not_dead_lettered(client, stack, monkeypatch):
    from sqlalchemy.exc import OperationalError

    real = handlers.reservations.reserve
    down = {"calls": 0}

    async def unavailable(*args, **kwargs):
        down["calls"] += 1
        if down["calls"] <= 6:  # far more failures than the five that would dead-letter
            raise OperationalError("stmt", {}, Exception("the database is unreachable"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(handlers.reservations, "reserve", unavailable)
    tx = await basket(client, {HOT: 2})

    await complete(client, tx)  # the shopper's wait may or may not outlast the outage

    await wait_until(lambda: down["calls"] > 6, timeout=8.0, what="Inventory retrying through the outage")
    assert stack.bus.records(ev.DEAD_LETTERS) == [], "an outage is not a poison event"
    await wait_until(lambda: adjustment_states(tx), timeout=8.0, what="the request being applied after the outage")
