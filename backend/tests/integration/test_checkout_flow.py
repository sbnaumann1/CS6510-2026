"""Integration test: the US1 checkout journey (T022).

Covers spec.md User Story 1 acceptance scenarios 1-4 and INV-4.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


async def test_acceptance_scenario_1_start_returns_id_and_open(client):
    r = await client.post("/transactions", json={"stationId": "station-01"})
    assert r.status_code == 201
    assert r.json()["status"] == "OPEN"
    assert r.json()["transactionId"].startswith("tx-")


async def test_acceptance_scenario_2_scan_returns_price_and_running_total(client):
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    r = await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000003"})

    assert r.json()["unitPrice"] == 1.55
    assert r.json()["runningTotal"] == 1.55


async def test_acceptance_scenario_3_complete_decrements_and_returns_receipt(
    client, session
):
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    for sku in ["SKU-000001"] * 3 + ["SKU-000002"] * 2:
        await client.post(f"/transactions/{tx}/items", json={"sku": sku})

    receipt = (await client.post(f"/transactions/{tx}/complete", json={})).json()
    assert receipt["itemCount"] == 5

    stock = dict(
        (
            await session.execute(
                text(
                    "SELECT sku, current_stock FROM inventory_stock"
                    " WHERE sku IN ('SKU-000001','SKU-000002')"
                )
            )
        ).all()
    )
    assert stock["SKU-000001"] == 97
    assert stock["SKU-000002"] == 98


async def test_acceptance_scenario_4_scan_after_complete_is_rejected(client):
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
    await client.post(f"/transactions/{tx}/complete", json={})

    r = await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
    assert r.status_code == 409
    assert r.json()["error"] == "TRANSACTION_NOT_OPEN"


async def test_stock_is_not_decremented_at_scan_time(client, session):
    """FR-005: inventory moves only at completion."""
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    for _ in range(4):
        await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000007"})

    before = await session.scalar(
        text("SELECT current_stock FROM inventory_stock WHERE sku = 'SKU-000007'")
    )
    assert before == 100

    await client.post(f"/transactions/{tx}/complete", json={})
    await session.commit()

    after = await session.scalar(
        text("SELECT current_stock FROM inventory_stock WHERE sku = 'SKU-000007'")
    )
    assert after == 96


async def test_inv4_denormalized_counters_match_line_items(client, session):
    tx = (await client.post("/transactions", json={"stationId": "station-09"})).json()[
        "transactionId"
    ]
    basket = ["SKU-000001", "SKU-000002", "SKU-000002", "SKU-000004"]
    for sku in basket:
        await client.post(f"/transactions/{tx}/items", json={"sku": sku})

    tx_id = int(tx.removeprefix("tx-"))
    row = (
        await session.execute(
            text(
                "SELECT t.item_count, t.running_total_cents,"
                "       count(i.id) AS n, coalesce(sum(i.unit_price_cents), 0) AS s"
                "  FROM transaction t LEFT JOIN transaction_item i"
                "    ON i.transaction_id = t.id"
                " WHERE t.id = :id GROUP BY t.id, t.item_count, t.running_total_cents"
            ),
            {"id": tx_id},
        )
    ).one()

    assert row.item_count == row.n == len(basket)
    assert row.running_total_cents == row.s


async def test_receipt_total_matches_running_total(client):
    tx = (await client.post("/transactions", json={"stationId": "station-01"})).json()[
        "transactionId"
    ]
    last = None
    for sku in ["SKU-000001", "SKU-000003", "SKU-000003"]:
        last = (await client.post(f"/transactions/{tx}/items", json={"sku": sku})).json()

    receipt = (await client.post(f"/transactions/{tx}/complete", json={})).json()
    assert receipt["totalAmount"] == pytest.approx(last["runningTotal"])
