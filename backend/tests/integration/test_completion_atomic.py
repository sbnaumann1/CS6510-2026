"""Integration test: the completion critical section is all-or-nothing (spec 006, T012).

Covers contracts/completion-statement.md: a short basket modifies nothing and names every
short SKU, and `repos.checkout.complete_basket` returns (ok, completed_at, short) with
alerts inserted in the same statement.
"""

from __future__ import annotations

from sqlalchemy import text

from checkout_data.repos import checkout as repo

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


async def _locked_open_tx(session) -> int:
    tx_id = await session.scalar(
        text("INSERT INTO transaction (station_id) VALUES ('station-01') RETURNING id")
    )
    assert (await repo.lock_transaction(session, tx_id)) is not None
    return tx_id


async def test_complete_basket_contract(reset_db, session):
    await session.execute(_SET_STOCK, {"n": 50, "sku": "SKU-000020"})
    await session.execute(_SET_STOCK, {"n": 50, "sku": "SKU-000021"})
    await session.commit()

    async with session.begin():
        tx_id = await _locked_open_tx(session)
        row = await repo.complete_basket(
            session, tx_id, ["SKU-000020", "SKU-000021"], [1, 1], 123, 50
        )
        assert row.ok is True
        assert row.short is None
        assert row.completed_at is not None

    alerts = (
        await session.execute(
            text(
                "SELECT sku, current_stock, threshold FROM low_stock_alert"
                " WHERE sku IN ('SKU-000020', 'SKU-000021') ORDER BY sku"
            )
        )
    ).all()
    assert [tuple(a) for a in alerts] == [("SKU-000020", 49, 50), ("SKU-000021", 49, 50)]
    tx = (
        await session.execute(
            text("SELECT status, total_amount_cents FROM transaction WHERE id = :id"),
            {"id": tx_id},
        )
    ).one()
    assert str(getattr(tx.status, "value", tx.status)) == "COMPLETED"
    assert tx.total_amount_cents == 123
    await session.commit()


async def test_complete_basket_short_returns_sorted_short_list(reset_db, session):
    await session.execute(_SET_STOCK, {"n": 5, "sku": "SKU-000020"})
    await session.execute(_SET_STOCK, {"n": 0, "sku": "SKU-000021"})
    await session.commit()

    await session.begin()
    tx_id = await _locked_open_tx(session)
    row = await repo.complete_basket(
        session, tx_id, ["SKU-000020", "SKU-000021"], [2, 1], 456, 50
    )
    assert row.ok is False
    assert row.completed_at is None
    assert row.short == ["SKU-000021"]
    await session.rollback()

    assert await _stock(session, ["SKU-000020", "SKU-000021"]) == {
        "SKU-000020": 5,
        "SKU-000021": 0,
    }
