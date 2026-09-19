"""Contract test: GET /transactions/{id} (T021)."""

from __future__ import annotations


async def test_get_returns_200_with_transaction_shape(client):
    tx = (await client.post("/transactions", json={"stationId": "station-03"})).json()[
        "transactionId"
    ]
    await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000002"})

    r = await client.get(f"/transactions/{tx}")
    assert r.status_code == 200

    body = r.json()
    for field in (
        "transactionId",
        "stationId",
        "status",
        "itemCount",
        "runningTotal",
        "startedAt",
    ):
        assert field in body, field

    assert body["transactionId"] == tx
    assert body["stationId"] == "station-03"
    assert body["status"] == "OPEN"
    assert body["itemCount"] == 1
    assert body["runningTotal"] == 1.20


async def test_status_becomes_completed(client):
    tx = (await client.post("/transactions", json={"stationId": "station-03"})).json()[
        "transactionId"
    ]
    await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000002"})
    await client.post(f"/transactions/{tx}/complete", json={})

    assert (await client.get(f"/transactions/{tx}")).json()["status"] == "COMPLETED"


async def test_unknown_transaction_is_404(client):
    r = await client.get("/transactions/tx-99999999")
    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == "TRANSACTION_NOT_FOUND"


async def test_malformed_id_is_404(client):
    r = await client.get("/transactions/garbage")
    assert r.status_code == 404
    assert r.json()["error"] == "TRANSACTION_NOT_FOUND"
