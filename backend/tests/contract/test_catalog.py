"""Contract test: GET /items (T017)."""

from __future__ import annotations


async def test_items_returns_200_with_contract_fields(client, catalog_size):
    r = await client.get("/items")
    assert r.status_code == 200
    body = r.json()

    assert set(body) == {"items"}
    assert len(body["items"]) == catalog_size

    first = body["items"][0]
    assert set(first) == {"sku", "name", "price"}
    assert isinstance(first["sku"], str)
    assert isinstance(first["name"], str)
    assert isinstance(first["price"], (int, float))


async def test_items_are_in_ascending_sku_order(client):
    # Load-bearing: the client's Zipf popularity rank is positional (research R12).
    skus = [i["sku"] for i in (await client.get("/items")).json()["items"]]
    assert skus == sorted(skus)


async def test_catalog_matches_the_mock_formulas(client):
    items = (await client.get("/items")).json()["items"]
    assert items[0] == {"sku": "SKU-000001", "name": "Item 1", "price": 0.85}
    assert items[1] == {"sku": "SKU-000002", "name": "Item 2", "price": 1.20}
