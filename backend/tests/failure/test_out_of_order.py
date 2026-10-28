"""Events can arrive out of order or late (spec edge cases, FR-005): a release that overtakes
its reserve refuses it, and a scan before the price snapshot has loaded is refused cleanly."""

from __future__ import annotations

import asyncio

from checkout_web import events as ev
from tests.failure._helpers import (
    RELEASE_REQUESTS,
    RESERVATION_REQUESTS,
    adjustment_states,
    assert_consistent,
    basket,
    complete,
    for_tx,
    of_type,
    status_of,
    stock_of,
    wait_until,
)

HOT = "SKU-000001"


async def test_a_release_that_overtakes_its_reserve_makes_the_reserve_refused(client, stack):
    tx = await basket(client, {HOT: 2})
    # The reserve is held back long enough for the shopper to give up and roll back, so the
    # release (published after it) reaches Inventory first.
    stack.bus.delay(RESERVATION_REQUESTS, 1.8, times=3)

    r = await complete(client, tx)

    assert r.status_code == 503
    assert await adjustment_states(tx) == [(1, "RELEASED")], "the release won"
    await wait_until(
        lambda: [e.type for e in stack.bus.events(ev.INVENTORY_STOCK)].count(ev.RESERVATION_REFUSED) == 3,
        timeout=6.0,
        what="the late reserves being refused",
    )
    assert await stock_of(HOT) == 100
    assert await status_of(client, tx) == "OPEN"
    await assert_consistent()


async def test_reserve_and_release_of_different_attempts_do_not_interfere(client, stack):
    """A release of attempt 1 delivered after attempt 2's reserve must not undo attempt 2."""
    tx = await basket(client, {HOT: 2})
    stack.bus.drop(RESERVATION_REQUESTS, times=3)
    assert (await complete(client, tx)).status_code == 503  # attempt 1 rolled back (epoch 1 released)
    stack.bus.clear_faults()

    assert (await complete(client, tx)).status_code == 200  # attempt 2 applies
    # A stale duplicate of attempt 1's release arrives now.
    from checkout_service import stock_events

    await stack.bus.publish(ev.CHECKOUT_COMPLETIONS, stock_events.release_request(tx, 1), key=tx)
    await wait_until(
        lambda: [e.data["epoch"] for e in stack.bus.events(ev.INVENTORY_STOCK) if e.type == ev.STOCK_RELEASED].count(1) >= 2,
        what="the stale release being answered",
    )

    assert await adjustment_states(tx) == [(1, "RELEASED"), (2, "APPLIED")], "attempt 2 is untouched"
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_scan_events_arriving_out_of_order_still_give_the_right_ranking(client, stack, analytics, poll_analytics):
    from analytics_service import popular_items

    stack.bus.reorder(of_type(ev.SCAN_RECORDED), times=3)  # three pairs swap places
    await basket(client, {HOT: 4, "SKU-000002": 2})  # six scans
    await poll_analytics()

    body = await popular_items.read(10)
    assert {i["sku"]: i["scanCount"] for i in body["items"]} == {HOT: 4, "SKU-000002": 2}
    assert [i["sku"] for i in body["items"]] == [HOT, "SKU-000002"]


async def test_a_completion_whose_reply_races_the_deadline_is_resolved_once(client, stack):
    """The reply arrives just as the wait ends. Whichever wins, the shopper gets exactly one
    answer and the stores agree."""
    tx = await basket(client, {HOT: 2})
    stack.bus.delay(of_type(ev.STOCK_RESERVED), 0.44, times=1)  # lands right at the first wait's end

    r = await complete(client, tx)

    assert r.status_code in (200, 503)
    await asyncio.sleep(0.6)  # let any late event settle
    await assert_consistent()
    assert await stock_of(HOT) == (98 if r.status_code == 200 else 100)
