"""TopicSnapshot: ready only after the marker and the items it counts (research R3)."""

from __future__ import annotations

import asyncio

from checkout_web.bus import MemoryBus
from checkout_web.events import CATALOG_ITEMS, CATALOG_PUBLISHED, ITEM_PUBLISHED, MARKER_KEY, make_event
from checkout_web.snapshot import TopicSnapshot


def _item(sku, price):
    return make_event(ITEM_PUBLISHED, "catalog", {"sku": sku, "name": f"name-{sku}", "priceCents": price})


def _marker(n):
    return make_event(CATALOG_PUBLISHED, "catalog", {"publishId": "p", "itemCount": n})


def _snap(bus):
    return TopicSnapshot(bus, name="t", parse=lambda sku, d: (d["name"], d["priceCents"]))


async def test_not_ready_before_marker_then_ready_with_all_items():
    bus = MemoryBus()
    snap = _snap(bus)
    task = asyncio.create_task(snap.run())
    for i in range(3):
        await bus.publish(CATALOG_ITEMS, _item(f"S{i}", 100 + i), key=f"S{i}")
    await asyncio.sleep(0.1)
    assert not snap.ready and snap.get("S0") == ("name-S0", 100)  # items seen, but no marker yet
    await bus.publish(CATALOG_ITEMS, _marker(3), key=MARKER_KEY)
    assert await snap.wait_ready(1.0)
    assert snap.get("S2") == ("name-S2", 102) and snap.get("nope") is None and len(snap) == 3
    task.cancel()


async def test_marker_before_all_items_waits_for_the_count():
    bus = MemoryBus()
    snap = _snap(bus)
    task = asyncio.create_task(snap.run())
    await bus.publish(CATALOG_ITEMS, _item("A", 1), key="A")
    await bus.publish(CATALOG_ITEMS, _marker(2), key=MARKER_KEY)
    await asyncio.sleep(0.1)
    assert not snap.ready
    await bus.publish(CATALOG_ITEMS, _item("B", 2), key="B")
    assert await snap.wait_ready(1.0)
    task.cancel()


async def test_republish_overwrites_and_duplicates_are_harmless():
    bus = MemoryBus()
    snap = _snap(bus)
    task = asyncio.create_task(snap.run())
    for _ in range(2):
        await bus.publish(CATALOG_ITEMS, _item("A", 1), key="A")
        await bus.publish(CATALOG_ITEMS, _marker(1), key=MARKER_KEY)
    assert await snap.wait_ready(1.0)
    await bus.publish(CATALOG_ITEMS, _item("A", 5), key="A")
    await asyncio.sleep(0.1)
    assert snap.get("A") == ("name-A", 5) and len(snap) == 1
    task.cancel()


async def test_a_late_starting_snapshot_rebuilds_from_the_retained_topic():
    bus = MemoryBus()
    for i in range(4):
        await bus.publish(CATALOG_ITEMS, _item(f"S{i}", i), key=f"S{i}")
    await bus.publish(CATALOG_ITEMS, _marker(4), key=MARKER_KEY)
    snap = _snap(bus)  # a service that starts (or restarts) after Catalog published
    task = asyncio.create_task(snap.run())
    assert await snap.wait_ready(1.0) and len(snap) == 4
    task.cancel()
