"""The scan relay (spec 008 research R8): committed scans reach `checkout.scans.v1` in scan-id
order, a hole is skipped only after its grace, a failed publish loses nothing, and a replaced
scan log is announced."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from checkout_service import repo
from checkout_service import seed as checkout_seed
from checkout_service.db import SessionLocal
from checkout_service.relay import ScanRelay
from checkout_web import events as ev
from checkout_web.bus import BrokerUnavailable
from tests.failure._helpers import basket


def scan_ids(bus) -> list[int]:
    return [e.data["scanId"] for e in bus.events(ev.CHECKOUT_SCANS) if e.type == ev.SCAN_RECORDED]


async def wait_for(pred, timeout=5.0):
    async def loop():
        while not pred():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


async def test_scans_reach_the_topic_in_id_order_with_their_transaction(client, stack):
    tx = await basket(client, {"SKU-000001": 2, "SKU-000002": 1})
    await wait_for(lambda: len(scan_ids(stack.bus)) == 3)

    events = [e for e in stack.bus.events(ev.CHECKOUT_SCANS) if e.type == ev.SCAN_RECORDED]
    assert [e.data["scanId"] for e in events] == [1, 2, 3]
    assert [e.data["sku"] for e in events] == ["SKU-000001", "SKU-000001", "SKU-000002"]
    assert all(e.data["transactionId"] == tx for e in events)
    assert len(stack.bus.records(ev.CHECKOUT_SCANS)) == 3, "one partition, in log order"


async def test_a_hole_is_skipped_only_after_its_grace_and_later_scans_wait_behind_it(client, stack):
    await stack.stop_service("checkout")  # run the relay by hand
    tx = (await client.post("/transactions", json={"stationId": "s"})).json()["transactionId"]
    for sku in ("SKU-000001", "SKU-000002"):
        await client.post(f"/transactions/{tx}/items", json={"sku": sku})
    async with SessionLocal() as s:  # an id that is consumed but never commits
        await s.execute(text("SELECT nextval(pg_get_serial_sequence('transaction_item', 'id'))"))
        await s.commit()
    for sku in ("SKU-000003", "SKU-000004"):
        await client.post(f"/transactions/{tx}/items", json={"sku": sku})  # ids 4 and 5

    relay = ScanRelay(gap_grace_s=0.2)
    assert await relay.run_pass() == 2 and scan_ids(stack.bus) == [1, 2], "3 is missing: 4 and 5 wait"
    assert await relay.run_pass() == 0, "still within the grace"
    await asyncio.sleep(0.25)
    assert await relay.run_pass() == 2
    assert scan_ids(stack.bus) == [1, 2, 4, 5], "3 is declared a permanent gap, then 4 and 5 follow"


async def test_a_failed_publish_loses_nothing_and_a_retry_publishes_all(client, stack):
    await stack.stop_service("checkout")
    await basket(client, {"SKU-000001": 3})

    relay = ScanRelay(gap_grace_s=0.1)
    stack.bus.fail_publish(times=1, predicate=lambda t, e, k: e.type == ev.SCAN_RECORDED)
    try:
        await relay.run_pass()
        raise AssertionError("the publish should have failed")
    except BrokerUnavailable:
        pass
    async with SessionLocal() as s:
        assert (await repo.relay_state(s))[0] == 0, "the position is saved only after the broker acknowledged"

    assert await relay.run_pass() == 3
    assert scan_ids(stack.bus) == [1, 2, 3]
    async with SessionLocal() as s:
        assert (await repo.relay_state(s))[0] == 3


async def test_a_restarted_relay_republishes_at_most_the_unacknowledged_scans(client, stack):
    await stack.stop_service("checkout")
    await basket(client, {"SKU-000001": 2})
    await ScanRelay(gap_grace_s=0.1).run_pass()
    assert scan_ids(stack.bus) == [1, 2]

    await basket(client, {"SKU-000002": 1})
    fresh = ScanRelay(gap_grace_s=0.1)  # a new process: it reads its position from the database
    assert await fresh.run_pass() == 1
    assert scan_ids(stack.bus) == [1, 2, 3], "nothing was published twice"


async def test_a_replaced_scan_log_is_announced_and_publishing_starts_over(client, stack):
    await basket(client, {"SKU-000001": 2})
    await wait_for(lambda: len(scan_ids(stack.bus)) == 2)

    checkout_seed.main([])  # Checkout's data is reset while the relay runs: a new scan log
    await wait_for(lambda: any(e.type == ev.SCAN_LOG_RESET for e in stack.bus.events(ev.CHECKOUT_SCANS)))
    reset = [e for e in stack.bus.events(ev.CHECKOUT_SCANS) if e.type == ev.SCAN_LOG_RESET][0]
    async with SessionLocal() as s:
        assert reset.data["logId"] == await repo.log_token(s)

    await basket(client, {"SKU-000009": 1})
    await wait_for(lambda: scan_ids(stack.bus)[-1] == 1 and len(scan_ids(stack.bus)) == 3)
