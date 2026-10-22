"""Checkout's scan log, read by Analytics (contracts/internal-apis.md "Checkout: read scans")."""

from __future__ import annotations

from httpx import AsyncClient

from tests._dispatch import MeshTransport


async def read(mesh: MeshTransport, **params):
    async with AsyncClient(transport=mesh, base_url="http://checkout:8000") as c:
        return await c.get("/internal/scans", params=params)


async def scan(client, tx: str, *skus: str) -> None:
    for sku in skus:
        assert (await client.post(f"/transactions/{tx}/items", json={"sku": sku})).status_code == 200


async def open_tx(client) -> str:
    return (await client.post("/transactions", json={"stationId": "s1"})).json()["transactionId"]


async def test_empty_log_reports_max_scan_id_zero(checkout_client, mesh):
    body = (await read(mesh, after=0)).json()
    assert body == {"scans": [], "maxScanId": 0}


async def test_scans_come_back_in_order_after_the_cursor(checkout_client, mesh):
    tx = await open_tx(checkout_client)
    await scan(checkout_client, tx, "SKU-000003", "SKU-000001", "SKU-000002")
    body = (await read(mesh, after=0)).json()
    assert [(s["scanId"], s["sku"]) for s in body["scans"]] == [
        (1, "SKU-000003"),
        (2, "SKU-000001"),
        (3, "SKU-000002"),
    ]
    assert body["maxScanId"] == 3
    tail = (await read(mesh, after=2)).json()
    assert [s["scanId"] for s in tail["scans"]] == [3]


async def test_limit_is_honoured_and_max_scan_id_ignores_it(checkout_client, mesh):
    tx = await open_tx(checkout_client)
    await scan(checkout_client, tx, *["SKU-000001"] * 5)
    body = (await read(mesh, after=0, limit=2)).json()
    assert [s["scanId"] for s in body["scans"]] == [1, 2]
    assert body["maxScanId"] == 5


async def test_limit_zero_returns_only_the_max_scan_id(checkout_client, mesh):
    tx = await open_tx(checkout_client)
    await scan(checkout_client, tx, "SKU-000001", "SKU-000002")
    assert (await read(mesh, after=0, limit=0)).json() == {"scans": [], "maxScanId": 2}


async def test_invalid_parameters_are_400(checkout_client, mesh):
    for params in ({}, {"after": "x"}, {"after": -1}, {"after": 0, "limit": 1001}, {"after": 0, "limit": "y"}):
        response = await read(mesh, **params)
        assert response.status_code == 400, params
        assert response.json()["error"] == "INVALID_REQUEST"


async def test_scans_in_a_transaction_that_is_never_completed_still_appear(checkout_client, mesh):
    """popular-items counts scans, not sales (007 research R5)."""
    tx = await open_tx(checkout_client)
    await scan(checkout_client, tx, "SKU-000004")
    body = (await read(mesh, after=0)).json()
    assert [s["sku"] for s in body["scans"]] == ["SKU-000004"]


async def test_the_public_gateway_does_not_expose_it(checkout_client):
    assert (await checkout_client.get("/internal/scans?after=0")).status_code == 404
