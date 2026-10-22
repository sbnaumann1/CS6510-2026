"""Analytics reads Checkout's scan log; neither can hurt the other's shoppers
(FR-016, SC-006, research R5).

Analytics keeps nothing on disk. While Checkout is unreachable it keeps serving what it has;
when Checkout returns it catches up and counts every scan once."""

from __future__ import annotations

import asyncio

import pytest

from analytics_service import pipeline, popular_items, state
from analytics_service.supervisor import PipelineSupervisor
from tests.failure._helpers import assert_consistent, basket, complete

A, B = "SKU-000001", "SKU-000002"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(pipeline, "UNAVAILABLE_BACKOFF_S", 0.01)


def counts(body) -> dict[str, int]:
    return {i["sku"]: i["scanCount"] for i in body["items"]}


async def test_checkout_keeps_serving_while_analytics_cannot_read_it(
    client, mesh, analytics, poll_analytics
):
    await basket(client, {A: 3})
    await poll_analytics()
    assert counts(await popular_items.read(10)) == {A: 3}

    # Analytics' reads fail for a while; shoppers are unaffected.
    mesh.fail("checkout", "/internal/scans", times=8, mode="error")
    tx = await basket(client, {A: 2, B: 4})
    assert (await complete(client, tx)).status_code == 200
    assert counts(await popular_items.read(10)) == {A: 3}, "still serving what it had"

    await poll_analytics()
    assert counts(await popular_items.read(10)) == {A: 5, B: 4}, "caught up, each scan once"
    await assert_consistent()


async def test_a_read_whose_response_was_lost_is_repeated_and_counted_once(
    client, mesh, analytics, poll_analytics
):
    mesh.fail("checkout", "/internal/scans", times=3, mode="drop_response")
    await basket(client, {A: 4, B: 1})

    await poll_analytics()

    assert counts(await popular_items.read(10)) == {A: 4, B: 1}
    assert state.cursor == 5


async def test_restarting_analytics_while_checkout_is_down_answers_empty_then_recovers(
    checkout_client, mesh, analytics
):
    await basket(checkout_client, {A: 3, B: 2})
    await asyncio.sleep(0.2)  # let the first pipeline read them
    assert counts(await popular_items.read(10)) == {A: 3, B: 2}

    # Analytics restarts (memory lost) while Checkout cannot be reached.
    await analytics.stop()
    state.reset()
    mesh.fail("checkout", "/internal/scans", times=10_000, mode="error")
    supervisor = PipelineSupervisor(backoff_s=0.01)
    task = asyncio.create_task(supervisor.run())
    try:
        await asyncio.sleep(0.1)
        body = (await checkout_client.get("/analytics/popular-items")).json()
        assert body["items"] == [] and body["windowEnd"] == 0, "empty window, not an error"
        assert supervisor.instances == 0, "still waiting for Checkout"

        mesh.clear_faults()  # Checkout is back
        deadline = asyncio.get_running_loop().time() + 5
        while counts(await popular_items.read(10)) != {A: 3, B: 2}:
            assert asyncio.get_running_loop().time() < deadline, "did not recover"
            await asyncio.sleep(0.02)
        assert supervisor.instances == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_scanning_never_waits_for_analytics(client, mesh, analytics):
    mesh.fail("checkout", "/internal/scans", times=10_000, mode="delay", delay=5.0)
    started = asyncio.get_running_loop().time()
    tx = await basket(client, {A: 5})
    assert (await complete(client, tx)).status_code == 200
    assert asyncio.get_running_loop().time() - started < 2.0
