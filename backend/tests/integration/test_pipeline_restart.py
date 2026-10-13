"""A restarted Analytics pipeline rebuilds its window from the shared database
(spec US2 scenario 2, edge case "Analytics restarts mid-run")."""

from __future__ import annotations

import asyncio
import logging

from tests.integration.test_persistent_pipeline import (
    open_tx,
    oracle,
    scan,
    snapshot_end,
    start_supervisor,
    wait_for,
)


async def test_restart_backfills_and_publishes_the_latest_window(client, caplog):
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

    # Analytics is "down": 900 more scans, max id 1600, latest boundary 1500.
    for i in range(900):
        await scan(client, tx, f"SKU-{i % 11 + 1:06d}")
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
