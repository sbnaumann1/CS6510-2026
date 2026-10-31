"""A Checkout worker drops a reply it does not own from the record key, before decoding it
(spec 009). A reply it does own still reaches its waiter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from checkout_service.stock_events import StockEvents
from checkout_web.bus import MemoryBus


def _rec(key):
    return SimpleNamespace(key=key)


def test_wanted_only_for_transactions_with_a_waiter_here():
    client = StockEvents(MemoryBus())
    assert not client._wanted(_rec("tx-7"))
    client._active[7] = 1
    assert client._wanted(_rec("tx-7"))
    assert not client._wanted(_rec("tx-8"))


def test_a_key_that_does_not_parse_is_decoded_not_dropped():
    client = StockEvents(MemoryBus())
    for key in (None, "garbage", "tx-"):
        assert client._wanted(_rec(key))


def test_active_count_is_released_when_the_ask_ends():
    async def go():
        client = StockEvents(MemoryBus())
        client._active[3] = 1
        # an ask that times out must leave no entry behind
        from checkout_service import stock_events
        try:
            await client._ask("tx-3", 1, stock_events.release_request("tx-3", 1),
                              frozenset({"inventory.stock-released"}), [0.01], "release")
        except Exception:
            pass
        return client._active

    active = asyncio.run(go())
    assert active.get(3) in (None, 1)  # the pre-set count of 1 is untouched, the ask's +1/-1 nets out
