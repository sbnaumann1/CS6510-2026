"""Integration test: the hopping window (T036).

Covers spec.md User Story 3 acceptance scenarios 1-2, SC-006, and the
fewer-than-N-items edge case.
"""

from __future__ import annotations

from sqlalchemy import text


async def _scan_many(client, distribution: dict[str, int]) -> None:
    """Scan a skewed distribution across several transactions."""
    for sku, n in distribution.items():
        remaining = n
        while remaining:
            take = min(remaining, 20)  # contract basket ceiling
            tx = (
                await client.post("/transactions", json={"stationId": "station-01"})
            ).json()["transactionId"]
            for _ in range(take):
                await client.post(f"/transactions/{tx}/items", json={"sku": sku})
            remaining -= take


async def test_ranking_reflects_true_scan_counts(client):
    await _scan_many(
        client, {"SKU-000001": 30, "SKU-000002": 20, "SKU-000003": 10, "SKU-000004": 5}
    )

    items = (await client.get("/analytics/popular-items?limit=4")).json()["items"]

    assert [i["sku"] for i in items] == [
        "SKU-000001",
        "SKU-000002",
        "SKU-000003",
        "SKU-000004",
    ]
    assert [i["scanCount"] for i in items] == [30, 20, 10, 5]
    assert [i["rank"] for i in items] == [1, 2, 3, 4]


async def test_names_come_from_the_catalog(client):
    await _scan_many(client, {"SKU-000005": 3})

    top = (await client.get("/analytics/popular-items")).json()["items"][0]
    assert top["sku"] == "SKU-000005"
    assert top["name"] == "Item 5"


async def test_edge_case_fewer_scans_than_window_size(client):
    """Before windowSize scans exist, return what there is; windowStart is 0."""
    await _scan_many(client, {"SKU-000001": 4, "SKU-000002": 2})

    body = (await client.get("/analytics/popular-items?limit=10")).json()
    assert body["windowStart"] == 0
    assert len(body["items"]) == 2
    assert body["items"][0]["scanCount"] == 4


async def test_empty_window_returns_no_items(client):
    body = (await client.get("/analytics/popular-items")).json()
    assert body["items"] == []
    assert body["windowStart"] == 0
    assert body["windowEnd"] == 0


async def test_ties_are_broken_by_sku_for_determinism(client):
    await _scan_many(client, {"SKU-000009": 5, "SKU-000008": 5, "SKU-000007": 5})

    items = (await client.get("/analytics/popular-items?limit=3")).json()["items"]
    assert [i["sku"] for i in items] == ["SKU-000007", "SKU-000008", "SKU-000009"]


async def test_window_slides_past_older_scans(client, session):
    """With a window of 1000, scan 1001 pushes scan 1 out (US3 scenario 1).

    Driven directly against the analytics service so the test stays fast; the
    HTTP path over 1000+ scans is covered by the load run.
    """
    from app.services import analytics

    # 1200 line items: the first 300 are SKU-000001, the rest SKU-000002.
    tx_id = await session.scalar(
        text("INSERT INTO transaction (station_id) VALUES ('bulk') RETURNING id")
    )
    await session.execute(
        text(
            "INSERT INTO transaction_item (transaction_id, sku, unit_price_cents)"
            " SELECT :tx, CASE WHEN g <= 300 THEN 'SKU-000001' ELSE 'SKU-000002' END, 100"
            "   FROM generate_series(1, 1200) AS g"
        ),
        {"tx": tx_id},
    )
    await session.commit()

    await analytics.recompute(session)
    body = await analytics.read(session, limit=10)

    # The window covers the most recent 1000 scans, so only 100 of the 300
    # SKU-000001 scans remain inside it.
    assert body["windowEnd"] - body["windowStart"] == 1000
    counts = {i["sku"]: i["scanCount"] for i in body["items"]}
    assert counts["SKU-000002"] == 900
    assert counts["SKU-000001"] == 100


async def test_recompute_is_idempotent(client, session):
    from app.services import analytics

    await _scan_many(client, {"SKU-000001": 10})
    await analytics.recompute(session)
    first = await analytics.read(session, limit=10)
    await analytics.recompute(session)
    second = await analytics.read(session, limit=10)

    assert first["items"] == second["items"]
    assert first["windowEnd"] == second["windowEnd"]


async def test_snapshot_is_a_single_row(client, session):
    from app.services import analytics

    await _scan_many(client, {"SKU-000001": 5})
    for _ in range(3):
        await analytics.recompute(session)

    n = await session.scalar(text("SELECT count(*) FROM popular_window_snapshot"))
    assert n == 1
