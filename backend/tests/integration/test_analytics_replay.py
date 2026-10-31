"""A restarted Analytics rebuilds its window from the scan topic's retained tail, with no call to
Checkout, and reflects every scan made while it was down (spec 008 US4, SC-008, research R8)."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from analytics_service import scan_source, state
from analytics_service.config import settings
from checkout_service.db import SessionLocal
from checkout_web import events as ev
from tests.failure._helpers import wait_until

SKUS = [f"SKU-{i:06d}" for i in range(1, 8)]


async def open_tx(client) -> str:
    return (await client.post("/transactions", json={"stationId": "replay"})).json()["transactionId"]


async def scan_many(client, tx: str, start: int, n: int) -> None:
    for i in range(start, start + n):
        r = await client.post(f"/transactions/{tx}/items", json={"sku": SKUS[i % len(SKUS)]})
        assert r.status_code == 200


async def sql_counts(first_id: int, last_id: int) -> dict[str, int]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                text("SELECT sku, count(*) AS n FROM transaction_item WHERE id BETWEEN :a AND :b GROUP BY sku"),
                {"a": first_id, "b": last_id},
            )
        ).all()
    return {r.sku: r.n for r in rows}


async def test_a_restarted_analytics_reflects_the_scans_made_while_it_was_down(
    client, stack, analytics, poll_analytics, poll_scan_events
):
    tx = await open_tx(client)
    await scan_many(client, tx, 0, 600)
    await poll_analytics()

    await stack.stop_service("analytics")  # its consumer is gone; its memory is about to be
    await scan_many(client, tx, 600, 700)  # 1 300 scans in all, 700 of them while it was down
    async with SessionLocal() as s:
        await wait_until(
            lambda: _relayed(s, 1300), timeout=15.0, what="the relay publishing to the scan topic"
        )
    state.reset()  # the process is gone
    await stack.start_service("analytics")  # a new consumer replays the topic's tail
    await analytics.restart()
    await poll_scan_events()

    async def published():
        return state.latest is not None and state.latest.window_end == 1000

    await wait_until(published, timeout=15.0, what="the window ending at scan 1000")
    got = {e["sku"]: e["scanCount"] for e in state.latest.ranking}
    assert got == await sql_counts(1, 1000), "the rebuilt window is exactly what the scan log says"


async def _relayed(session, upto: int) -> bool:
    from checkout_service import repo

    return (await repo.relay_state(session))[0] >= upto


async def test_the_rebuild_replays_only_the_retained_tail_and_never_calls_checkout(
    client, stack, analytics, poll_analytics
):
    tx = await open_tx(client)
    await scan_many(client, tx, 0, 2300)
    await poll_analytics()
    window = settings.popular_window_size

    await stack.stop_service("checkout")  # Checkout is not needed to rebuild
    await stack.stop_service("analytics")
    state.reset()
    await stack.start_service("analytics")
    await analytics.restart()

    async def rebuilt():
        return state.latest is not None and state.latest.window_end == 2000

    await wait_until(rebuilt, timeout=15.0, what="the window ending at scan 2000")
    source = scan_source.default_source()
    assert len(source._rows) <= 2 * window, "it replayed the last 2 x window scans, not the whole topic"
    assert source.highest_applied == 2300
    got = {e["sku"]: e["scanCount"] for e in state.latest.ranking}
    assert got == await sql_counts(1001, 2000)
    assert len(stack.bus.records(ev.CHECKOUT_SCANS)) == 2300, "every scan is still on the topic"
