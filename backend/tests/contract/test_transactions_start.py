"""Contract test: POST /transactions (T018)."""

from __future__ import annotations

import re

import pytest


async def test_start_returns_201_not_200(client):
    # The load client asserts 201 specifically (contracts/README.md).
    r = await client.post("/transactions", json={"stationId": "station-01"})
    assert r.status_code == 201


async def test_start_body_has_contract_fields(client):
    r = await client.post("/transactions", json={"stationId": "station-07"})
    body = r.json()

    for field in ("transactionId", "stationId", "status", "itemCount", "runningTotal", "startedAt"):
        assert field in body, field

    assert body["stationId"] == "station-07"
    assert body["status"] == "OPEN"
    assert body["itemCount"] == 0
    assert body["runningTotal"] == 0
    assert re.fullmatch(r"tx-\d+", body["transactionId"])


async def test_transaction_ids_are_unique(client):
    ids = set()
    for _ in range(5):
        r = await client.post("/transactions", json={"stationId": "station-01"})
        ids.add(r.json()["transactionId"])
    assert len(ids) == 5


@pytest.mark.parametrize(
    "payload",
    [{}, {"stationId": ""}, {"stationId": 42}, {"stationId": None}],
)
async def test_invalid_station_id_is_400_invalid_request(client, payload):
    r = await client.post("/transactions", json=payload)
    assert r.status_code == 400
    body = r.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == "INVALID_REQUEST"
    assert isinstance(body["message"], str)


async def test_malformed_json_is_400_invalid_request(client):
    r = await client.post(
        "/transactions",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_REQUEST"
