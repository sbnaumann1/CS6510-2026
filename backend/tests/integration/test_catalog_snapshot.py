"""Reference data by event (spec 008 research R3): Catalog publishes its items once at startup,
and Checkout, Inventory and Analytics build their snapshots from that topic. An unknown SKU is
a 404; a snapshot that has not loaded yet is a clean 503; nobody calls Catalog."""

from __future__ import annotations

from httpx import AsyncClient

from analytics_service import catalog_snapshot as analytics_snap
from catalog_service import main as catalog_main
from catalog_service.main import load as load_catalog
from checkout_service import catalog_snapshot as checkout_snap
from checkout_service import main as checkout_main
from checkout_web import events as ev
from checkout_web.bus import MemoryBus
from inventory_service import catalog_snapshot as inventory_snap
from tests._dispatch import build_apps, build_dispatch_app


async def test_every_service_builds_its_snapshot_from_the_published_catalog(client, stack):
    items = (await client.get("/items")).json()["items"]
    assert checkout_snap.get_snapshot().ready
    assert inventory_snap.get_snapshot().ready
    assert analytics_snap.get_snapshot().ready

    # Checkout prices a scan from its snapshot of the event, equal to what /items shows.
    tx = (await client.post("/transactions", json={"stationId": "s"})).json()["transactionId"]
    first = items[0]
    r = await client.post(f"/transactions/{tx}/items", json={"sku": first["sku"]})
    assert r.status_code == 200
    assert r.json()["unitPrice"] == first["price"] and r.json()["name"] == first["name"]

    names = await inventory_snap.get_snapshot().names([first["sku"], "SKU-NOPE"])
    assert names == {first["sku"]: first["name"], "SKU-NOPE": "SKU-NOPE"}, "an unknown SKU is its own name"


async def test_catalog_publishes_every_item_then_a_marker_with_the_count(client, stack):
    items = (await client.get("/items")).json()["items"]
    records = [r.event() for r in stack.bus.records(ev.CATALOG_ITEMS)]
    published = [e for e in records if e.type == ev.ITEM_PUBLISHED]
    markers = [e for e in records if e.type == ev.CATALOG_PUBLISHED]
    assert {e.data["sku"] for e in published} == {i["sku"] for i in items}
    assert len(markers) == 1 and markers[0].data["itemCount"] == len(items)
    assert records[-1].type == ev.CATALOG_PUBLISHED, "the marker comes after every item"
    assert all(r.key for r in stack.bus.records(ev.CATALOG_ITEMS)), "keyed by SKU (the marker by __marker__)"


async def test_a_cold_snapshot_is_a_clean_503_and_an_unknown_sku_a_404(reset_db):
    bus = MemoryBus()
    tasks = await checkout_main.start_background(bus, outbox_sweeper=False, scan_relay=False)
    catalog_tasks: list = []
    try:
        async with AsyncClient(app=build_dispatch_app(build_apps()), base_url="http://test") as c:
            tx = (await c.post("/transactions", json={"stationId": "s"})).json()["transactionId"]

            # Catalog has not published: this is not "no such SKU", and no price may be guessed.
            r = await c.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
            assert r.status_code == 503 and r.json()["error"] == "INTERNAL_ERROR"
            assert (await c.get(f"/transactions/{tx}")).json()["itemCount"] == 0, "nothing was recorded"

            load_catalog()
            catalog_tasks = await catalog_main.start_background(bus)
            assert await checkout_snap.get_snapshot().wait_ready(3.0)

            ok = await c.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
            assert ok.status_code == 200
            unknown = await c.post(f"/transactions/{tx}/items", json={"sku": "SKU-NOPE"})
            assert unknown.status_code == 404 and unknown.json()["error"] == "SKU_NOT_FOUND"
    finally:
        await checkout_main.stop_background(tasks)
        await catalog_main.stop_background(catalog_tasks)
