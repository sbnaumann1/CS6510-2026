"""Contract test: POST /transactions/{id}/complete (T020)."""

from __future__ import annotations

import pytest
from sqlalchemy import text


async def _basket(client, skus) -> str:
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    for sku in skus:
        await client.post(f"/transactions/{tx}/items", json={"sku": sku})
    return tx


async def test_complete_returns_200_with_contract_fields(client):
    tx = await _basket(client, ["SKU-000001", "SKU-000001", "SKU-000002"])
    r = await client.post(f"/transactions/{tx}/complete", json={})
    assert r.status_code == 200

    body = r.json()
    for field in (
        "transactionId",
        "stationId",
        "itemCount",
        "totalAmount",
        "startedAt",
        "completedAt",
        "lines",
    ):
        assert field in body, field

    assert body["itemCount"] == 3
    assert body["totalAmount"] == pytest.approx(0.85 * 2 + 1.20)


async def test_receipt_lines_group_by_sku(client):
    tx = await _basket(client, ["SKU-000001", "SKU-000001", "SKU-000002"])
    lines = (await client.post(f"/transactions/{tx}/complete", json={})).json()["lines"]

    assert len(lines) == 2
    by_sku = {line["sku"]: line for line in lines}
    assert by_sku["SKU-000001"]["quantity"] == 2
    assert by_sku["SKU-000002"]["quantity"] == 1
    for line in lines:
        assert set(line) == {"sku", "name", "unitPrice", "quantity"}


async def test_inv5_total_equals_sum_of_lines(client):
    tx = await _basket(client, ["SKU-000001"] * 3 + ["SKU-000002"] * 2 + ["SKU-000003"])
    body = (await client.post(f"/transactions/{tx}/complete", json={})).json()

    expected = sum(line["unitPrice"] * line["quantity"] for line in body["lines"])
    assert body["totalAmount"] == pytest.approx(expected)


async def test_complete_accepts_empty_json_body(client):
    # The load client posts {} with content-type application/json.
    tx = await _basket(client, ["SKU-000001"])
    r = await client.post(
        f"/transactions/{tx}/complete",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200


async def test_complete_does_not_require_a_body(client):
    tx = await _basket(client, ["SKU-000001"])
    r = await client.post(f"/transactions/{tx}/complete")
    assert r.status_code == 200


async def test_unknown_transaction_is_404(client):
    r = await client.post("/transactions/tx-99999999/complete", json={})
    assert r.status_code == 404
    assert r.json()["error"] == "TRANSACTION_NOT_FOUND"


async def test_double_complete_is_409_transaction_not_open(client):
    tx = await _basket(client, ["SKU-000001"])
    await client.post(f"/transactions/{tx}/complete", json={})

    r = await client.post(f"/transactions/{tx}/complete", json={})
    assert r.status_code == 409
    assert r.json()["error"] == "TRANSACTION_NOT_OPEN"


async def test_empty_basket_is_409_empty_basket(client):
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    r = await client.post(f"/transactions/{tx}/complete", json={})
    assert r.status_code == 409
    assert r.json()["error"] == "EMPTY_BASKET"


async def test_insufficient_stock_is_409_and_decrements_nothing(client, session):
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 1 WHERE sku = 'SKU-000005'")
    )
    await session.commit()

    tx = await _basket(client, ["SKU-000005", "SKU-000005", "SKU-000001"])
    r = await client.post(f"/transactions/{tx}/complete", json={})
    assert r.status_code == 409
    assert r.json()["error"] == "INSUFFICIENT_STOCK"

    # Nothing is decremented — not the short SKU, and not the plentiful one.
    rows = dict(
        (
            await session.execute(
                text(
                    "SELECT sku, current_stock FROM inventory_stock"
                    " WHERE sku IN ('SKU-000005','SKU-000001')"
                )
            )
        ).all()
    )
    assert rows["SKU-000005"] == 1
    assert rows["SKU-000001"] == 100


async def test_insufficient_stock_leaves_transaction_open(client, session):
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 0 WHERE sku = 'SKU-000006'")
    )
    await session.commit()

    tx = await _basket(client, ["SKU-000006"])
    assert (await client.post(f"/transactions/{tx}/complete", json={})).status_code == 409

    # Deliberate: a retry after restock would succeed (data-model.md).
    assert (await client.get(f"/transactions/{tx}")).json()["status"] == "OPEN"
