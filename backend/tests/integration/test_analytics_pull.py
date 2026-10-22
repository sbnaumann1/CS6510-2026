"""Analytics against Checkout through the mesh (spec 007 research R5): it reads scans in
order, rebuilds after losing its memory, copes with holes, and re-bootstraps when Checkout's
data is reset. It has no store of its own."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from sqlalchemy import text

import analytics_service
from analytics_service import state
from analytics_service.supervisor import PipelineSupervisor
from checkout_service.db import SessionLocal


async def open_tx(client) -> str:
    return (await client.post("/transactions", json={"stationId": "pull"})).json()["transactionId"]


async def scan(client, tx: str, sku: str) -> None:
    assert (await client.post(f"/transactions/{tx}/items", json={"sku": sku})).status_code == 200


async def wait_until(pred, timeout=10.0):
    async def loop():
        while not pred():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


async def test_analytics_reads_scans_in_order_and_serves_them(client, poll_analytics):
    tx = await open_tx(client)
    for sku in ("SKU-000002", "SKU-000001", "SKU-000002"):
        await scan(client, tx, sku)
    await poll_analytics()
    items = (await client.get("/analytics/popular-items")).json()["items"]
    assert [(i["sku"], i["scanCount"]) for i in items] == [("SKU-000002", 2), ("SKU-000001", 1)]
    assert state.cursor == 3


async def test_a_restarted_analytics_rebuilds_the_same_window(client, analytics, poll_analytics):
    tx = await open_tx(client)
    for i in range(1200):
        await scan(client, tx, f"SKU-{i % 7 + 1:06d}")
    await poll_analytics()
    await wait_until(lambda: state.latest is not None and state.latest.window_end == 1000)
    before = state.latest

    sup = await analytics.restart()  # memory lost; Checkout's scan log is not
    assert sup.instances == 1
    await wait_until(lambda: state.latest is not None and state.latest.window_end == 1000)

    assert state.latest.ranking == before.ranking
    assert (state.latest.window_start, state.latest.window_end) == (before.window_start, 1000)


async def test_a_hole_that_never_commits_is_skipped_after_the_grace_period(
    checkout_client, mesh
):
    from tests.integration.test_persistent_pipeline import scan as pscan

    tx = await open_tx(checkout_client)
    for sku in ("SKU-000001", "SKU-000002"):
        await pscan(checkout_client, tx, sku)
    # A scan id that is consumed but never commits: a rolled-back insert leaves a hole.
    async with SessionLocal() as s:
        await s.execute(text("SELECT nextval(pg_get_serial_sequence('transaction_item', 'id'))"))
        await s.commit()
    for sku in ("SKU-000003", "SKU-000004"):
        await pscan(checkout_client, tx, sku)  # ids 4 and 5

    sup = PipelineSupervisor(grace_s=0.1)
    task = asyncio.create_task(sup.run())
    try:
        await wait_until(lambda: state.cursor == 5, timeout=5)
        assert [(sku, n) for sku, n in state.window.counts(10)] == [
            ("SKU-000001", 1), ("SKU-000002", 1), ("SKU-000003", 1), ("SKU-000004", 1)
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_checkout_data_reset_triggers_a_rebootstrap(checkout_client, mesh):
    tx = await open_tx(checkout_client)
    for i in range(30):
        await scan(checkout_client, tx, f"SKU-{i % 3 + 1:06d}")
    sup = PipelineSupervisor(backoff_s=0.05)
    task = asyncio.create_task(sup.run())
    try:
        await wait_until(lambda: state.cursor == 30)
        # Checkout's database is reset while Analytics keeps running.
        async with SessionLocal() as s:
            await s.execute(text("TRUNCATE transaction_item, transaction RESTART IDENTITY CASCADE"))
            await s.commit()
        await wait_until(lambda: sup.instances >= 2, timeout=5)
        assert state.cursor == 0 and len(state.window) == 0
        tx = await open_tx(checkout_client)
        await scan(checkout_client, tx, "SKU-000009")
        await wait_until(lambda: state.cursor == 1)
        assert state.window.counts(5) == [("SKU-000009", 1)]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rereading_the_same_scans_counts_them_once(client, analytics, poll_analytics):
    tx = await open_tx(client)
    for _ in range(5):
        await scan(client, tx, "SKU-000001")
    await poll_analytics()
    await analytics.restart(forget=False)  # starts over from Checkout, memory not cleared first
    await poll_analytics()
    items = (await client.get("/analytics/popular-items")).json()["items"]
    assert [(i["sku"], i["scanCount"]) for i in items] == [("SKU-000001", 5)]


def test_analytics_has_no_database_to_open():
    """No engine, no session factory, no database driver: nothing to connect with."""
    import pathlib
    import re

    root = pathlib.Path(inspect.getfile(analytics_service)).parent
    importing = re.compile(r"^\s*(?:from|import)\s+(sqlalchemy|asyncpg|psycopg2?)\b", re.M)
    for path in root.glob("*.py"):
        assert not importing.search(path.read_text()), f"{path.name} imports a database library"
    for forbidden in ("engine", "SessionLocal", "try_advisory_lock"):
        assert not hasattr(analytics_service.supervisor, forbidden), forbidden
