"""Contract test: GET /analytics/popular-items (T035)."""

from __future__ import annotations

import pytest


async def _scan(client, sku: str, times: int = 1) -> None:
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    for _ in range(times):
        await client.post(f"/transactions/{tx}/items", json={"sku": sku})


async def test_popular_items_returns_200_with_window_metadata(client):
    await _scan(client, "SKU-000001", 3)

    r = await client.get("/analytics/popular-items")
    assert r.status_code == 200

    body = r.json()
    assert set(body) == {
        "windowSize",
        "slideInterval",
        "windowStart",
        "windowEnd",
        "computedAt",
        "items",
    }
    assert body["windowSize"] == 1000
    assert body["slideInterval"] == 500
    assert isinstance(body["windowStart"], int)
    assert isinstance(body["windowEnd"], int)
    assert isinstance(body["computedAt"], str)


async def test_items_have_contract_fields(client):
    await _scan(client, "SKU-000002", 4)

    items = (await client.get("/analytics/popular-items")).json()["items"]
    assert items, "expected at least one ranked item"

    first = items[0]
    assert set(first) == {"sku", "name", "scanCount", "rank"}
    assert isinstance(first["sku"], str)
    assert isinstance(first["name"], str)
    assert isinstance(first["scanCount"], int)
    assert isinstance(first["rank"], int)


async def test_ranks_are_contiguous_from_one(client):
    for sku, n in [("SKU-000001", 5), ("SKU-000002", 3), ("SKU-000003", 1)]:
        await _scan(client, sku, n)

    items = (await client.get("/analytics/popular-items")).json()["items"]
    assert [i["rank"] for i in items] == list(range(1, len(items) + 1))


async def test_limit_is_honored(client):
    for i in range(1, 6):
        await _scan(client, f"SKU-{i:06d}", i)

    items = (await client.get("/analytics/popular-items?limit=2")).json()["items"]
    assert len(items) == 2


async def test_limit_larger_than_available_returns_all(client):
    await _scan(client, "SKU-000001", 2)

    items = (await client.get("/analytics/popular-items?limit=500")).json()["items"]
    assert 0 < len(items) <= 500


async def test_default_limit_is_ten(client):
    for i in range(1, 16):
        await _scan(client, f"SKU-{i:06d}", 1)

    items = (await client.get("/analytics/popular-items")).json()["items"]
    assert len(items) == 10


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "2.5", ""])
async def test_invalid_limit_is_400_invalid_request(client, bad):
    r = await client.get(f"/analytics/popular-items?limit={bad}")
    assert r.status_code == 400
    body = r.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == "INVALID_REQUEST"


async def test_window_bounds_span_at_most_window_size(client):
    await _scan(client, "SKU-000001", 5)

    body = (await client.get("/analytics/popular-items")).json()
    assert body["windowEnd"] - body["windowStart"] <= body["windowSize"]
