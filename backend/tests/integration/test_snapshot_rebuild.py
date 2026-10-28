"""Snapshots rebuild from the event history alone (spec 008 US4 scenario 3, FR-010): a service
that starts fresh against an existing topic becomes ready without Catalog taking part, and a
Catalog restart (which republishes) changes nothing."""

from __future__ import annotations

from analytics_service import catalog_snapshot as analytics_snap
from checkout_service import catalog_snapshot as checkout_snap
from checkout_web import events as ev
from inventory_service import catalog_snapshot as inventory_snap
from tests.failure._helpers import wait_until


def contents(snap) -> dict:
    return dict(snap._snap._items)


async def test_a_service_starting_fresh_is_ready_from_the_topic_without_catalog(stack, client):
    items = (await client.get("/items")).json()["items"]
    await stack.stop_service("catalog")  # Catalog takes no part in what follows
    await stack.stop_service("checkout")
    assert not any(t for t in stack.tasks.get("catalog", []))

    await stack.start_service("checkout")  # a brand-new snapshot, empty at first
    await wait_until(lambda: checkout_snap.get_snapshot().ready, what="the snapshot rebuilt from the topic")

    snap = checkout_snap.get_snapshot()
    assert len(snap._snap) == len(items)
    first = items[0]
    assert (await snap.get(first["sku"]))[1] == round(first["price"] * 100)


async def test_a_catalog_restart_republishes_and_changes_no_snapshot(stack, client):
    before = {
        "checkout": contents(checkout_snap.get_snapshot()),
        "inventory": contents(inventory_snap.get_snapshot()),
        "analytics": contents(analytics_snap.get_snapshot()),
    }
    markers_before = sum(e.type == ev.CATALOG_PUBLISHED for e in stack.bus.events(ev.CATALOG_ITEMS))

    await stack.stop_service("catalog")
    await stack.start_service("catalog")  # a restart: the whole list again, then a second marker
    await wait_until(
        lambda: sum(e.type == ev.CATALOG_PUBLISHED for e in stack.bus.events(ev.CATALOG_ITEMS)) == markers_before + 1,
        what="the second publication",
    )

    assert contents(checkout_snap.get_snapshot()) == before["checkout"]
    assert contents(inventory_snap.get_snapshot()) == before["inventory"]
    assert contents(analytics_snap.get_snapshot()) == before["analytics"]
    assert checkout_snap.get_snapshot().ready and inventory_snap.get_snapshot().ready


async def test_a_new_service_started_before_catalog_waits_then_loads(reset_db):
    """The reverse order: a consumer that starts first is simply not ready, then loads the
    moment Catalog publishes."""
    from catalog_service import main as catalog_main
    from catalog_service.main import load as load_catalog
    from checkout_service import main as checkout_main
    from checkout_web.bus import MemoryBus

    bus = MemoryBus()
    tasks = await checkout_main.start_background(bus, outbox_sweeper=False, scan_relay=False)
    try:
        assert not checkout_snap.get_snapshot().ready
        load_catalog()
        catalog_tasks = await catalog_main.start_background(bus)
        try:
            assert await checkout_snap.get_snapshot().wait_ready(3.0)
        finally:
            await catalog_main.stop_background(catalog_tasks)
    finally:
        await checkout_main.stop_background(tasks)
