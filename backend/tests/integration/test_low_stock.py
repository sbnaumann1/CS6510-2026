"""Integration test: low-stock alerts (T031).

Covers spec.md User Story 2 acceptance scenarios 1-3 and SC-005.
"""

from __future__ import annotations

from sqlalchemy import text


async def _buy(client, sku: str, units: int = 1) -> int:
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    for _ in range(units):
        await client.post(f"/transactions/{tx}/items", json={"sku": sku})
    return (await client.post(f"/transactions/{tx}/complete", json={})).status_code


async def test_acceptance_1_crossing_the_threshold_generates_an_alert(client, session):
    # Stock 50, threshold 50: buying one unit drops to 49, which is below.
    # (spec.md US2 scenario 1 originally said 51 -> 50, but 50 is *at* the
    # threshold, not below it; FR-006, data-model.md and MockServer all use
    # `stock < threshold`. The spec has been corrected to match.)
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 50 WHERE sku = 'SKU-000030'")
    )
    await session.commit()

    assert await _buy(client, "SKU-000030") == 200

    n = await session.scalar(
        text("SELECT count(*) FROM low_stock_alert WHERE sku = 'SKU-000030'")
    )
    assert n == 1

    alerts = (await client.get("/inventory/low-stock")).json()["alerts"]
    assert any(a["sku"] == "SKU-000030" and a["currentStock"] == 49 for a in alerts)


async def test_no_alert_when_the_threshold_is_not_crossed(client, session):
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 80 WHERE sku = 'SKU-000031'")
    )
    await session.commit()

    await _buy(client, "SKU-000031")

    n = await session.scalar(
        text("SELECT count(*) FROM low_stock_alert WHERE sku = 'SKU-000031'")
    )
    assert n == 0


async def test_one_alert_row_per_descent_not_per_completion(client, session):
    """The crossing condition fires once; later purchases below the threshold do
    not append more rows (data-model.md)."""
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 51 WHERE sku = 'SKU-000032'")
    )
    await session.commit()

    for _ in range(5):
        await _buy(client, "SKU-000032")

    n = await session.scalar(
        text("SELECT count(*) FROM low_stock_alert WHERE sku = 'SKU-000032'")
    )
    assert n == 1


async def test_multi_unit_basket_crossing_fires_once(client, session):
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 55 WHERE sku = 'SKU-000033'")
    )
    await session.commit()

    assert await _buy(client, "SKU-000033", units=10) == 200

    rows = (
        await session.execute(
            text(
                "SELECT current_stock, threshold FROM low_stock_alert"
                " WHERE sku = 'SKU-000033'"
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].current_stock == 45
    assert rows[0].threshold == 50


async def test_sc005_alert_is_committed_with_the_decrement(client, session):
    """No query can observe a below-threshold item without its alert row."""
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 50 WHERE sku = 'SKU-000034'")
    )
    await session.commit()

    await _buy(client, "SKU-000034")

    row = (
        await session.execute(
            text(
                "SELECT s.current_stock, count(a.id) AS alerts"
                "  FROM inventory_stock s"
                "  LEFT JOIN low_stock_alert a ON a.sku = s.sku"
                " WHERE s.sku = 'SKU-000034'"
                " GROUP BY s.current_stock"
            )
        )
    ).one()
    assert row.current_stock == 49
    assert row.alerts == 1


async def test_acceptance_2_query_returns_all_items_below_threshold(client, session):
    await session.execute(
        text(
            "UPDATE inventory_stock SET current_stock = 20"
            " WHERE sku IN ('SKU-000035','SKU-000036')"
        )
    )
    await session.commit()

    alerts = (await client.get("/inventory/low-stock?threshold=100")).json()["alerts"]
    skus = {a["sku"] for a in alerts}
    assert {"SKU-000035", "SKU-000036"} <= skus
    assert all(a["currentStock"] < 100 for a in alerts)


async def test_acceptance_3_stock_never_goes_negative(client, session):
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 2 WHERE sku = 'SKU-000037'")
    )
    await session.commit()

    assert await _buy(client, "SKU-000037", units=5) == 409

    stock = await session.scalar(
        text("SELECT current_stock FROM inventory_stock WHERE sku = 'SKU-000037'")
    )
    assert stock == 2


async def test_alert_reflects_live_stock_not_the_historical_row(client, session):
    """The endpoint computes from live stock; the alert row only supplies the
    timestamp (data-model.md)."""
    await session.execute(
        text("UPDATE inventory_stock SET current_stock = 51 WHERE sku = 'SKU-000038'")
    )
    await session.commit()

    await _buy(client, "SKU-000038")  # 51 -> 50, not yet below: no alert row
    await _buy(client, "SKU-000038", units=10)  # 50 -> 40 crosses; row records 40

    alert = next(
        a
        for a in (await client.get("/inventory/low-stock")).json()["alerts"]
        if a["sku"] == "SKU-000038"
    )
    assert alert["currentStock"] == 40
