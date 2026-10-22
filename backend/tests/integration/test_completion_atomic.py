"""Integration test: the stock critical section is all-or-nothing (spec 006, T012).

Covers the reservation statement that moved from Checkout to Inventory (spec 007): a short
basket modifies nothing and names every short SKU, and `reserve.reserve` decrements, alerts
and records the adjustment in the same statement.
"""

from __future__ import annotations

from sqlalchemy import text

from inventory_service import reserve

_STOCK = text("SELECT sku, current_stock FROM inventory_stock WHERE sku = ANY(:skus) ORDER BY sku")
_SET_STOCK = text("UPDATE inventory_stock SET current_stock = :n WHERE sku = :sku")
_ALERTS = text("SELECT count(*) FROM low_stock_alert")


async def _stock(session, skus: list[str]) -> dict[str, int]:
    rows = (await session.execute(_STOCK, {"skus": skus})).all()
    await session.commit()
    return {r.sku: r.current_stock for r in rows}


async def test_short_basket_modifies_nothing_and_lists_every_short_sku(client, session):
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    for sku in ("SKU-000001", "SKU-000003", "SKU-000003", "SKU-000010"):
        assert (await client.post(f"/transactions/{tx}/items", json={"sku": sku})).status_code == 200

    await session.execute(_SET_STOCK, {"n": 1, "sku": "SKU-000003"})
    await session.execute(_SET_STOCK, {"n": 0, "sku": "SKU-000010"})
    await session.commit()
    skus = ["SKU-000001", "SKU-000003", "SKU-000010"]
    before = await _stock(session, skus)
    alerts_before = await session.scalar(_ALERTS)
    await session.commit()

    resp = await client.post(f"/transactions/{tx}/complete", json={})
    assert resp.status_code == 409
    assert resp.json()["error"] == "INSUFFICIENT_STOCK"
    assert resp.json()["message"] == "Insufficient stock for SKU-000003, SKU-000010."

    assert await _stock(session, skus) == before
    assert await session.scalar(_ALERTS) == alerts_before
    await session.commit()
    assert (await client.get(f"/transactions/{tx}")).json()["status"] == "OPEN"

    await session.execute(_SET_STOCK, {"n": 100, "sku": "SKU-000003"})
    await session.execute(_SET_STOCK, {"n": 100, "sku": "SKU-000010"})
    await session.commit()
    assert (await client.post(f"/transactions/{tx}/complete", json={})).status_code == 200
    after = await _stock(session, skus)
    assert after == {
        "SKU-000001": before["SKU-000001"] - 1,
        "SKU-000003": 100 - 2,
        "SKU-000010": 100 - 1,
    }


async def test_reserve_contract(reset_db, inventory_session):
    s = inventory_session
    await s.execute(_SET_STOCK, {"n": 50, "sku": "SKU-000020"})
    await s.execute(_SET_STOCK, {"n": 50, "sku": "SKU-000021"})
    await s.commit()

    async with s.begin():
        result = await reserve.reserve(
            s, 7, [("SKU-000020", 1), ("SKU-000021", 1)], threshold=50
        )
    assert result == reserve.Reserved(replayed=False)

    alerts = (
        await s.execute(
            text(
                "SELECT sku, current_stock, threshold FROM low_stock_alert"
                " WHERE sku IN ('SKU-000020', 'SKU-000021') ORDER BY sku"
            )
        )
    ).all()
    assert [tuple(a) for a in alerts] == [("SKU-000020", 49, 50), ("SKU-000021", 49, 50)]
    adjustment = (
        await s.execute(text("SELECT state, lines FROM stock_adjustment WHERE transaction_id = 7"))
    ).one()
    assert adjustment.state == "APPLIED"
    assert sorted((l["sku"], l["qty"]) for l in adjustment.lines) == [
        ("SKU-000020", 1),
        ("SKU-000021", 1),
    ]
    await s.commit()


async def test_reserve_short_returns_sorted_short_list_and_modifies_nothing(
    reset_db, inventory_session
):
    s = inventory_session
    await s.execute(_SET_STOCK, {"n": 5, "sku": "SKU-000020"})
    await s.execute(_SET_STOCK, {"n": 0, "sku": "SKU-000021"})
    await s.commit()

    async with s.begin():
        result = await reserve.reserve(
            s, 8, [("SKU-000021", 1), ("SKU-000020", 2)], threshold=50
        )
    assert result == reserve.Short(["SKU-000021"])

    assert await _stock(s, ["SKU-000020", "SKU-000021"]) == {
        "SKU-000020": 5,
        "SKU-000021": 0,
    }
    n = await s.scalar(text("SELECT count(*) FROM stock_adjustment"))
    await s.commit()
    assert n == 0, "a refused reservation leaves no adjustment record"
