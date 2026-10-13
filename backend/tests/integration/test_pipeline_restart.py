"""A restarted Analytics pipeline rebuilds its window from the shared database
(spec US2 scenario 2, edge cases "Analytics restarts mid-run" and "Several
Analytics instances run at once")."""

from __future__ import annotations

import asyncio
import logging

from analytics_service.supervisor import PipelineSupervisor
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


async def test_standby_takes_over_without_losing_scans(client):
    """Two Analytics processes: only the lock holder runs a pipeline; when it
    goes away the standby bootstraps and its first window is complete."""
    tx = await open_tx(client)
    _, leader_task = await start_supervisor()

    standby_published: list = []
    standby = PipelineSupervisor(retry_s=0.05, on_publish=standby_published.append)
    standby_task = asyncio.create_task(standby.run())
    try:
        for i in range(600):
            await scan(client, tx, f"SKU-{i % 4 + 1:06d}")

        async def at_500():
            return await snapshot_end() == 500

        await wait_for(at_500)
        assert standby.instances == 0, "standby must not run a pipeline while the leader holds the lock"

        # The leader dies; scans continue while nobody is computing.
        leader_task.cancel()
        await asyncio.gather(leader_task, return_exceptions=True)
        for i in range(900):
            await scan(client, tx, f"SKU-{i % 6 + 1:06d}")

        async def at_1500():
            return await snapshot_end() == 1500

        await wait_for(at_1500)
        assert standby.instances == 1
        # The standby takes the lock as soon as the leader goes; it may do so
        # mid-stream (re-publishing 500, then following live) — either way every
        # window it publishes must be complete and exact.
        ends = [r.window_end for r in standby_published]
        assert ends == sorted(set(ends)) and ends[-1] == 1500
        for r in standby_published:
            assert r.ranking == await oracle(r.window_start, r.window_end), r.window_end
    finally:
        standby_task.cancel()
        await asyncio.gather(standby_task, return_exceptions=True)
