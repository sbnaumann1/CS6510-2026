"""Catalog down (spec 008 US4 scenario 2): Checkout and Inventory rebuild their catalog
snapshots from Catalog's retained events, so they can restart while Catalog is stopped and
still price scans, complete transactions and answer low-stock."""

from __future__ import annotations

from checkout_service import catalog_snapshot as checkout_snap
from inventory_service import catalog_snapshot as inventory_snap
from tests.failure._helpers import assert_consistent, basket, complete, set_stock, stock_of, wait_until


async def test_services_restarted_while_catalog_is_down_still_serve(client, stack, session):
    await set_stock(session, "SKU-000003", 40)  # below the default threshold: low-stock has an entry
    await stack.stop_service("catalog")  # Catalog is down...
    await stack.stop_service("checkout")
    await stack.stop_service("inventory")
    await stack.start_service("checkout")  # ...and both consumers of its data restart
    await stack.start_service("inventory")

    await wait_until(lambda: checkout_snap.get_snapshot().ready, what="Checkout's snapshot from the topic")
    await wait_until(lambda: inventory_snap.get_snapshot().ready, what="Inventory's snapshot from the topic")

    tx = await basket(client, {"SKU-000001": 2, "SKU-000002": 1})  # priced from the rebuilt snapshot
    r = await complete(client, tx)
    assert r.status_code == 200
    assert await stock_of("SKU-000001") == 98

    low = (await client.get("/inventory/low-stock")).json()
    assert [a["sku"] for a in low["alerts"]] == ["SKU-000003"]
    assert low["alerts"][0]["name"] == "Item 3", "the name comes from the snapshot, not from a call"
    await assert_consistent()


async def test_unknown_skus_are_still_a_404_while_catalog_is_down(client, stack):
    await stack.stop_service("catalog")
    tx = (await client.post("/transactions", json={"stationId": "s"})).json()["transactionId"]
    r = await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-NOPE"})
    assert r.status_code == 404 and r.json()["error"] == "SKU_NOT_FOUND"
