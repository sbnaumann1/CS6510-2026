"""Contract test: GET /inventory/low-stock (T030)."""

from __future__ import annotations

import pytest
from sqlalchemy import text


async def test_low_stock_returns_200_with_contract_fields(client, session):
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 3 WHERE sku = 'SKU-000020'")
    )
    await session.commit()

    r = await client.get("/inventory/low-stock")
    assert r.status_code == 200

    body = r.json()
    assert set(body) == {"threshold", "generatedAt", "alerts"}
    assert isinstance(body["threshold"], int)
    assert isinstance(body["generatedAt"], str)

    alert = next(a for a in body["alerts"] if a["sku"] == "SKU-000020")
    assert set(alert) == {"sku", "name", "currentStock", "threshold", "triggeredAt"}
    assert alert["currentStock"] == 3
    assert alert["name"] == "Item 20"


async def test_threshold_query_override_is_echoed(client):
    r = await client.get("/inventory/low-stock?threshold=250")
    assert r.status_code == 200
    assert r.json()["threshold"] == 250


async def test_threshold_override_widens_the_result(client, catalog_size):
    # Every item starts at 100 units, so a threshold of 250 matches all of them.
    body = (await client.get("/inventory/low-stock?threshold=250")).json()
    assert len(body["alerts"]) == catalog_size


async def test_default_threshold_is_used_when_absent(client):
    body = (await client.get("/inventory/low-stock")).json()
    assert body["threshold"] == 50


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "1.5", ""])
async def test_invalid_threshold_is_400_invalid_request(client, bad):
    r = await client.get(f"/inventory/low-stock?threshold={bad}")
    assert r.status_code == 400
    body = r.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == "INVALID_REQUEST"


async def test_alerts_are_ordered_by_triggered_at_then_sku(client, session):
    await session.execute(
        text(
            "UPDATE inventory_stock SET current_stock = 10"
            " WHERE sku IN ('SKU-000021','SKU-000022','SKU-000023')"
        )
    )
    await session.commit()

    alerts = (await client.get("/inventory/low-stock")).json()["alerts"]
    keys = [(a["triggeredAt"], a["sku"]) for a in alerts]
    assert keys == sorted(keys)
