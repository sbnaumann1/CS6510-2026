"""The consumer loop: ordering per key, retry, dead letters, transient outages (research R9)."""

from __future__ import annotations

import asyncio

from checkout_web.bus import MemoryBus, Transient
from checkout_web.consumer_loop import run_consumer
from checkout_web.events import CHECKOUT_COMPLETIONS, DEAD_LETTERS, STOCK_RELEASE_REQUESTED, make_event


def _ev(n: int):
    return make_event(STOCK_RELEASE_REQUESTED, "checkout", {"transactionId": f"tx-{n}", "epoch": 1})


async def _run(bus, handler, **kw):
    consumer = bus.consumer(CHECKOUT_COMPLETIONS, group="g")
    task = asyncio.create_task(
        run_consumer(consumer, handler, name="g", bus=bus, backoff_s=0.01, transient_backoff_s=0.02,
                     poll_ms=20, **kw)
    )
    return task


async def _until(pred, timeout=2.0):
    end = asyncio.get_event_loop().time() + timeout
    while not pred():
        assert asyncio.get_event_loop().time() < end, "condition not reached"
        await asyncio.sleep(0.01)


async def test_a_failing_handler_is_retried_then_succeeds():
    bus = MemoryBus()
    calls = []

    async def handler(rec):
        calls.append(rec.offset)
        if len(calls) == 1:
            raise RuntimeError("once")

    await bus.publish(CHECKOUT_COMPLETIONS, _ev(1), key="tx-1")
    task = await _run(bus, handler)
    await _until(lambda: len(calls) == 2)
    task.cancel()
    assert bus.records(DEAD_LETTERS) == []


async def test_five_failures_dead_letter_and_the_next_record_is_processed():
    bus = MemoryBus()
    seen = []

    async def handler(rec):
        if rec.event().data["transactionId"] == "tx-1":
            raise RuntimeError("always")
        seen.append(rec.event().data["transactionId"])

    await bus.publish(CHECKOUT_COMPLETIONS, _ev(1), key="tx-1")
    await bus.publish(CHECKOUT_COMPLETIONS, _ev(1), key="tx-1")  # same key: must wait behind the poison one
    await bus.publish(CHECKOUT_COMPLETIONS, _ev(2), key="tx-2")
    task = await _run(bus, handler)
    await _until(lambda: "tx-2" in seen and len(bus.records(DEAD_LETTERS)) == 2)
    task.cancel()
    dead = bus.events(DEAD_LETTERS)[0].data
    assert dead["sourceTopic"] == CHECKOUT_COMPLETIONS and dead["attempts"] == 5 and dead["consumer"] == "g"
    assert "always" in dead["error"]


async def test_malformed_bytes_are_dead_lettered_at_once():
    bus = MemoryBus()
    await bus._append(CHECKOUT_COMPLETIONS, _ev(1), "tx-1")
    # corrupt the stored value
    rec = bus.records(CHECKOUT_COMPLETIONS)[0]
    bus._logs[CHECKOUT_COMPLETIONS][rec.partition][0] = type(rec)(
        rec.topic, rec.partition, rec.offset, rec.key, b"{not json", rec.headers
    )

    async def handler(rec):
        rec.event()

    task = await _run(bus, handler)
    await _until(lambda: len(bus.records(DEAD_LETTERS)) == 1)
    task.cancel()
    dead = bus.events(DEAD_LETTERS)[0].data
    assert dead["attempts"] == 1 and dead["event"] == "{not json"


async def test_transient_errors_wait_and_never_dead_letter():
    bus = MemoryBus()
    state = {"down": True, "ok": 0, "tries": 0}

    async def handler(rec):
        state["tries"] += 1
        if state["down"]:
            raise Transient("database unreachable")
        state["ok"] += 1

    await bus.publish(CHECKOUT_COMPLETIONS, _ev(1), key="tx-1")
    task = await _run(bus, handler)
    await _until(lambda: state["tries"] >= 8)  # far more than max_attempts
    assert bus.records(DEAD_LETTERS) == []
    state["down"] = False
    await _until(lambda: state["ok"] == 1)
    task.cancel()


async def test_same_key_is_sequential_and_different_keys_overlap():
    bus = MemoryBus()
    active = {"now": 0, "max": 0}
    order = []

    async def handler(rec):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)
        order.append((rec.key, rec.event().data["epoch"]))
        active["now"] -= 1

    for epoch in (1, 2, 3):
        await bus.publish(
            CHECKOUT_COMPLETIONS,
            make_event(STOCK_RELEASE_REQUESTED, "checkout", {"transactionId": "tx-1", "epoch": epoch}),
            key="tx-1",
        )
    for n in (2, 3, 4):
        await bus.publish(CHECKOUT_COMPLETIONS, _ev(n), key=f"tx-{n}")
    task = await _run(bus, handler)
    await _until(lambda: len(order) == 6)
    task.cancel()
    assert [e for k, e in order if k == "tx-1"] == [1, 2, 3]
    assert active["max"] > 1
