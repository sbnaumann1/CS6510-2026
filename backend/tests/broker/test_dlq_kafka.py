"""A poison record on a real topic is dead-lettered and does not block the partition."""

from __future__ import annotations

from aiokafka import AIOKafkaProducer

from checkout_web import events as ev
from tests.broker.conftest import BOOTSTRAP
from tests.failure._helpers import assert_consistent, basket, complete, stock_of, wait_until

HOT = "SKU-000001"


async def test_malformed_bytes_become_a_dead_letter_and_later_requests_are_served(client, stack):
    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP)
    await producer.start()
    try:
        await producer.send_and_wait(ev.CHECKOUT_COMPLETIONS, b"{definitely not an event", key=b"tx-999")
    finally:
        await producer.stop()

    consumer = stack.bus.consumer(ev.DEAD_LETTERS, start="beginning")
    await consumer.start()
    dead = []

    async def got_one():
        dead.extend(await consumer.poll(300))
        return bool(dead)

    await wait_until(got_one, timeout=20.0, what="the dead letter on the real topic")
    await consumer.stop()
    letter = dead[0].event()
    assert letter.type == ev.DEAD_LETTER and letter.data["sourceTopic"] == ev.CHECKOUT_COMPLETIONS
    assert letter.data["consumer"] == "inventory" and letter.data["event"] == "{definitely not an event"

    tx = await basket(client, {HOT: 2})
    assert (await complete(client, tx)).status_code == 200
    assert await stock_of(HOT) == 98
    await assert_consistent()
