"""The completion flow on real Kafka: the whole system in-process on a real broker."""

from __future__ import annotations

from checkout_web import events as ev
from tests.failure._helpers import (
    adjustment_states,
    assert_consistent,
    basket,
    complete,
    set_stock,
    stock_of,
    wait_until,
)

HOT = "SKU-000001"


async def read_topic(bus, topic: str) -> list:
    """Everything on the topic, read with a fresh group-less consumer."""
    consumer = bus.consumer(topic, start="beginning")
    await consumer.start()
    records = []
    for _ in range(6):
        batch = await consumer.poll(500)
        records += batch
        if not batch and records:
            break
    await consumer.stop()
    return records


async def test_a_completion_over_real_kafka_keeps_request_and_reply_on_one_partition(client, stack):
    tx = await basket(client, {HOT: 2, "SKU-000002": 1})

    r = await complete(client, tx)

    assert r.status_code == 200
    assert await stock_of(HOT) == 98
    requests = await read_topic(stack.bus, ev.CHECKOUT_COMPLETIONS)
    replies = await read_topic(stack.bus, ev.INVENTORY_STOCK)
    assert [x.event().type for x in requests] == [ev.STOCK_RESERVATION_REQUESTED]
    assert [x.event().type for x in replies] == [ev.STOCK_RESERVED]
    assert requests[0].key == tx == replies[0].key
    assert requests[0].partition == replies[0].partition, "same key, same partition, in order"
    assert replies[0].headers["x-request-id"] == requests[0].headers["x-request-id"]
    await assert_consistent()


async def test_a_refusal_and_a_retry_over_real_kafka(client, stack, session):
    await set_stock(session, HOT, 1)
    tx = await basket(client, {HOT: 2})
    assert (await complete(client, tx)).status_code == 409
    assert await adjustment_states(tx) == [(1, "REJECTED")]

    await set_stock(session, HOT, 5)
    assert (await complete(client, tx)).status_code == 200
    assert await adjustment_states(tx) == [(1, "REJECTED"), (2, "APPLIED")]
    await assert_consistent()


async def test_many_concurrent_completions_over_real_kafka_never_oversell(client, stack, session):
    import asyncio

    await set_stock(session, HOT, 20)
    txs = [await basket(client, {HOT: 2}) for _ in range(15)]
    results = await asyncio.gather(*(complete(client, tx) for tx in txs))
    assert sum(r.status_code == 200 for r in results) == 10
    assert await stock_of(HOT) == 0
    await wait_until(lambda: True)
    await assert_consistent()
