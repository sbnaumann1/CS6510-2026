"""A restarted Analytics pipeline rebuilds its window from Checkout's scan log (spec US2
scenario 2, edge case "Analytics restarts mid-run"; 007 research R5).

Analytics keeps nothing on disk, so a restart loses its memory and bootstraps again from
`maxScanId`. There is no leader election: a second instance would simply compute the same
windows from the same source.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from analytics_service import state
from analytics_service.supervisor import PipelineSupervisor
from tests.integration.test_persistent_pipeline import (
    open_tx,
    oracle,
    scan,
    snapshot_end,
    start_supervisor,
    wait_for,
)


@pytest.fixture
def client(checkout_client):
    return checkout_client


async def test_restart_backfills_and_publishes_the_latest_window(client, caplog, poll_scan_events):
    caplog.set_level(logging.INFO, logger="analytics")
    tx = await open_tx(client)

    _, task = await start_supervisor()
    for i in range(700):
        await scan(client, tx, f"SKU-{i % 5 + 1:06d}")

    async def at_500():
        return await snapshot_end() == 500

    await wait_for(at_500)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    state.reset()  # the process is gone: so is its memory

    # Analytics is "down": 900 more scans, max id 1600, latest boundary 1500. They are on the
    # scan topic (the relay publishes them whether or not anyone is reading).
    for i in range(900):
        await scan(client, tx, f"SKU-{i % 11 + 1:06d}")
    await poll_scan_events()
    caplog.clear()

    published: list = []
    sup, task = await start_supervisor(on_publish=published.append)
    try:
        assert "pipeline started bootstrap_from=500" in caplog.text

        async def at_1500():
            return await snapshot_end() == 1500

        await wait_for(at_1500)
        # The first window after bootstrap is complete, never a partial 1000.
        assert published[0].window_end == 1500
        assert published[0].ranking == await oracle(500, 1500)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_second_instance_computes_the_same_windows(client):
    """No leader election: two pipelines over the same source each compute the same exact
    windows (they only duplicate work). Their published windows are compared through the
    `on_publish` callbacks."""
    tx = await open_tx(client)
    first_published: list = []
    second_published: list = []
    _, first_task = await start_supervisor(on_publish=first_published.append)
    second = PipelineSupervisor(on_publish=second_published.append)
    second_task = asyncio.create_task(second.run())
    try:
        for i in range(600):
            await scan(client, tx, f"SKU-{i % 4 + 1:06d}")

        async def both_at_500():
            return (
                bool(first_published)
                and bool(second_published)
                and first_published[-1].window_end == second_published[-1].window_end == 500
            )

        await wait_for(both_at_500)
        assert first_published[-1].ranking == second_published[-1].ranking == await oracle(0, 500)
    finally:
        for task in (first_task, second_task):
            task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
