"""MemoryBus keeps the properties the design relies on, and its fault hooks work (research R13)."""

from __future__ import annotations

import asyncio

import pytest

from checkout_web.bus import BrokerUnavailable, MemoryBus
from checkout_web.events import (
    CHECKOUT_COMPLETIONS,
    CHECKOUT_SCANS,
    STOCK_RELEASE_REQUESTED,
    STOCK_RESERVATION_REQUESTED,
    make_event,
)


def _ev(n: int, etype: str = STOCK_RELEASE_REQUESTED):
    return make_event(etype, "checkout", {"transactionId": f"tx-{n}", "epoch": 1}, correlation_id=f"r{n}")


async def _drain(consumer, expect: int, timeout: float = 1.0):
    got = []
    deadline = asyncio.get_event_loop().time() + timeout
    while len(got) < expect and asyncio.get_event_loop().time() < deadline:
        got += await consumer.poll(50)
    return got


async def test_same_key_stays_in_order_on_one_partition():
    bus = MemoryBus()
    for n in range(20):
        await bus.publish(CHECKOUT_COMPLETIONS, _ev(n), key="tx-7")
    parts = {r.partition for r in bus.records(CHECKOUT_COMPLETIONS)}
    assert len(parts) == 1
    got = await _drain(bus.consumer(CHECKOUT_COMPLETIONS), 20)
    assert [r.event().correlation_id for r in got] == [f"r{n}" for n in range(20)]


async def test_group_resumes_after_the_last_commit():
    bus = MemoryBus()
    for n in range(5):
        await bus.publish(CHECKOUT_SCANS, _ev(n))
    c1 = bus.consumer(CHECKOUT_SCANS, group="g")
    first = await _drain(c1, 5)
    assert len(first) == 5
    await c1.commit()
    await bus.publish(CHECKOUT_SCANS, _ev(5))
    # a new consumer in the same group (a restart) sees only what was not committed
    c2 = bus.consumer(CHECKOUT_SCANS, group="g")
    assert [r.event().correlation_id for r in await _drain(c2, 1)] == ["r5"]


async def test_start_end_sees_only_later_records_and_tail_replays_the_last_n():
    bus = MemoryBus()
    for n in range(10):
        await bus.publish(CHECKOUT_SCANS, _ev(n))
    tail = bus.consumer(CHECKOUT_SCANS, start=3)
    end = bus.consumer(CHECKOUT_SCANS, start="end")
    await bus.publish(CHECKOUT_SCANS, _ev(10))
    assert [r.event().correlation_id for r in await _drain(tail, 4)] == ["r7", "r8", "r9", "r10"]
    assert [r.event().correlation_id for r in await _drain(end, 1)] == ["r10"]


async def test_drop_duplicate_delay_reorder_fail():
    bus = MemoryBus()
    only = lambda n: (lambda topic, e, key: e.correlation_id == f"r{n}")

    bus.drop(only(1))
    await bus.publish(CHECKOUT_SCANS, _ev(1))
    assert bus.records(CHECKOUT_SCANS) == []

    bus.duplicate(only(2), copies=2)
    await bus.publish(CHECKOUT_SCANS, _ev(2))
    assert len(bus.records(CHECKOUT_SCANS)) == 3

    bus.delay(only(3), 0.05)
    await bus.publish(CHECKOUT_SCANS, _ev(3))
    assert len(bus.records(CHECKOUT_SCANS)) == 3
    await asyncio.sleep(0.15)
    assert len(bus.records(CHECKOUT_SCANS)) == 4

    bus.reorder(lambda t, e, k: e.type == STOCK_RESERVATION_REQUESTED)
    await bus.publish(CHECKOUT_COMPLETIONS, _ev(4, STOCK_RESERVATION_REQUESTED), key="tx-4")
    await bus.publish(CHECKOUT_COMPLETIONS, _ev(5, STOCK_RESERVATION_REQUESTED), key="tx-4")
    order = [r.event().correlation_id for r in bus.records(CHECKOUT_COMPLETIONS)]
    assert order == ["r5", "r4"]

    bus.fail_publish(times=1)
    with pytest.raises(BrokerUnavailable):
        await bus.publish(CHECKOUT_SCANS, _ev(6))
    await bus.publish(CHECKOUT_SCANS, _ev(6))  # the fault is spent


async def test_crash_before_commit_redelivers_the_batch():
    bus = MemoryBus()
    await bus.publish(CHECKOUT_SCANS, _ev(1))
    bus.crash_before_commit("g")
    c = bus.consumer(CHECKOUT_SCANS, group="g")
    assert len(await _drain(c, 1)) == 1
    await c.commit()  # lost
    assert len(await _drain(c, 1)) == 1  # redelivered
    await c.commit()  # this one lands
    assert await c.poll(20) == []
