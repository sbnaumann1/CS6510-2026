"""Analytics rebuilds its window from the real scan topic's retained tail, with no call to
Checkout (SC-008)."""

from __future__ import annotations

from analytics_service import popular_items, state
from checkout_service import repo
from checkout_service.db import SessionLocal
from tests.failure._helpers import basket, wait_until

A, B = "SKU-000001", "SKU-000002"


def counts(body) -> dict[str, int]:
    return {i["sku"]: i["scanCount"] for i in body["items"]}


async def test_a_restarted_analytics_replays_the_topic_and_counts_each_scan_once(
    client, stack, analytics, poll_analytics
):
    await basket(client, {A: 3, B: 2})
    await poll_analytics()
    assert counts(await popular_items.read(10)) == {A: 3, B: 2}

    await stack.stop_service("analytics")
    await basket(client, {B: 4})  # scanned while Analytics is down: the scans wait on the topic

    async def relayed():  # the relay publishes asynchronously: wait until all nine are on the topic
        async with SessionLocal() as s:
            return (await repo.relay_state(s))[0] == 9

    await wait_until(relayed, timeout=15.0, what="the relay publishing the scans to the real broker")
    await stack.stop_service("checkout")  # and Checkout is not needed for the rebuild
    state.reset()
    await stack.start_service("analytics")  # a new consumer reads the retained tail

    await analytics.restart()

    async def rebuilt():
        return counts(await popular_items.read(10)) == {A: 3, B: 6}

    await wait_until(rebuilt, timeout=15.0, what="the window rebuilt from the real topic")
