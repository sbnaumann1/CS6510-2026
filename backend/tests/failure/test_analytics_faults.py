"""Checkout and Analytics cannot hurt each other (FR-016, SC-008; spec 008 research R8).

Analytics keeps nothing on disk and Checkout does not know it exists: scans reach it as events
on a topic. While Analytics is down the scans wait on the topic; when it returns it replays the
tail, rebuilds its window and counts every scan once. Analytics does not need Checkout to be
running at all."""

from __future__ import annotations

import asyncio

from analytics_service import popular_items, state
from checkout_web import events as ev
from tests.failure._helpers import assert_consistent, basket, complete, of_type

A, B = "SKU-000001", "SKU-000002"


def counts(body) -> dict[str, int]:
    return {i["sku"]: i["scanCount"] for i in body["items"]}


async def test_checkout_keeps_serving_while_analytics_is_down_and_it_catches_up(
    client, stack, analytics, poll_analytics
):
    await basket(client, {A: 3})
    await poll_analytics()
    assert counts(await popular_items.read(10)) == {A: 3}

    await stack.stop_service("analytics")  # Analytics' consumer is gone; shoppers are unaffected
    tx = await basket(client, {A: 2, B: 4})
    assert (await complete(client, tx)).status_code == 200
    assert counts(await popular_items.read(10)) == {A: 3}, "still serving what it had"

    await stack.start_service("analytics")  # it replays the topic's tail: no call to Checkout
    await analytics.restart()
    await poll_analytics()
    assert counts(await popular_items.read(10)) == {A: 5, B: 4}, "caught up, each scan once"
    await assert_consistent()


async def test_duplicated_and_reordered_scan_events_are_counted_once(client, stack, analytics, poll_analytics):
    stack.bus.duplicate(of_type(ev.SCAN_RECORDED), copies=2)
    stack.bus.reorder(of_type(ev.SCAN_RECORDED), times=2)  # two pairs swap places
    await basket(client, {A: 4, B: 1})
    await basket(client, {B: 1})  # six scans: an even number, so no swap is left half-done

    await poll_analytics()

    assert counts(await popular_items.read(10)) == {A: 4, B: 2}
    assert state.cursor == 6


async def test_analytics_restarts_and_rebuilds_while_checkout_is_down(client, stack, analytics, poll_analytics):
    await basket(client, {A: 3, B: 2})
    await poll_analytics()
    assert counts(await popular_items.read(10)) == {A: 3, B: 2}

    # Both stop; only Analytics comes back. It needs nothing from Checkout (it reads the topic).
    await stack.stop_service("checkout")
    await stack.stop_service("analytics")
    state.reset()  # the process is gone: so is its memory
    await stack.start_service("analytics")

    await analytics.restart()
    deadline = asyncio.get_running_loop().time() + 5
    while counts(await popular_items.read(10)) != {A: 3, B: 2}:
        assert asyncio.get_running_loop().time() < deadline, "did not recover"
        await asyncio.sleep(0.02)


async def test_scanning_never_waits_for_analytics(client, stack, analytics):
    stack.bus.delay(of_type(ev.SCAN_RECORDED), 5.0, times=10_000)
    started = asyncio.get_running_loop().time()
    tx = await basket(client, {A: 5})
    assert (await complete(client, tx)).status_code == 200
    assert asyncio.get_running_loop().time() - started < 2.0
