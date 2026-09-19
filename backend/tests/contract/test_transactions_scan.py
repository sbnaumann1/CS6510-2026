"""Contract test: POST /transactions/{id}/items (T019)."""

from __future__ import annotations

import pytest


async def _open_tx(client) -> str:
    r = await client.post("/transactions", json={"stationId": "station-01"})
    return r.json()["transactionId"]


async def test_scan_returns_200_with_contract_fields(client):
    tx = await _open_tx(client)
    r = await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
    assert r.status_code == 200

    body = r.json()
    for field in ("transactionId", "sku", "name", "unitPrice", "itemCount", "runningTotal"):
        assert field in body, field

    assert body["transactionId"] == tx
    assert body["sku"] == "SKU-000001"
    assert body["name"] == "Item 1"
    assert body["unitPrice"] == 0.85
    assert body["itemCount"] == 1
    assert body["runningTotal"] == 0.85


async def test_running_total_accumulates_across_scans(client):
    tx = await _open_tx(client)
    seen = []
    for sku in ("SKU-000001", "SKU-000001", "SKU-000002"):
        seen.append((await client.post(f"/transactions/{tx}/items", json={"sku": sku})).json())

    assert [s["itemCount"] for s in seen] == [1, 2, 3]
    assert seen[-1]["runningTotal"] == pytest.approx(0.85 + 0.85 + 1.20)


@pytest.mark.parametrize("payload", [{}, {"sku": ""}, {"sku": 7}])
async def test_invalid_sku_payload_is_400(client, payload):
    tx = await _open_tx(client)
    r = await client.post(f"/transactions/{tx}/items", json=payload)
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_REQUEST"


async def test_unknown_sku_is_404_sku_not_found(client):
    tx = await _open_tx(client)
    r = await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-999999"})
    assert r.status_code == 404
    assert r.json()["error"] == "SKU_NOT_FOUND"


async def test_unknown_transaction_is_404_transaction_not_found(client):
    r = await client.post("/transactions/tx-99999999/items", json={"sku": "SKU-000001"})
    assert r.status_code == 404
    assert r.json()["error"] == "TRANSACTION_NOT_FOUND"


async def test_malformed_transaction_id_is_404_transaction_not_found(client):
    # Anything not matching tx-<digits> is rejected without a query (research R5).
    r = await client.post("/transactions/not-an-id/items", json={"sku": "SKU-000001"})
    assert r.status_code == 404
    assert r.json()["error"] == "TRANSACTION_NOT_FOUND"


async def test_scan_after_completion_is_409_transaction_not_open(client):
    tx = await _open_tx(client)
    await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
    await client.post(f"/transactions/{tx}/complete", json={})

    r = await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
    assert r.status_code == 409
    assert r.json()["error"] == "TRANSACTION_NOT_OPEN"


async def test_sku_not_found_takes_precedence_over_transaction_not_open(client):
    # error-catalog.md precedence: SKU_NOT_FOUND (3) before TRANSACTION_NOT_OPEN (4).
    tx = await _open_tx(client)
    await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
    await client.post(f"/transactions/{tx}/complete", json={})

    r = await client.post(f"/transactions/{tx}/items", json={"sku": "NOPE"})
    assert r.status_code == 404
    assert r.json()["error"] == "SKU_NOT_FOUND"
