"""Integration test: the hopping window (T036).

Covers spec.md User Story 3 acceptance scenarios 1-2, SC-006, and the
fewer-than-N-items edge case.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from analytics_service import state


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


async def _wait_published(end: int, timeout: float = 10.0) -> None:
    async def loop():
        while state.latest is None or state.latest.window_end < end:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


async def test_window_slides_past_older_scans(client, checkout_session, poll_analytics):
    """With a window of 1000, later scans push older ones out (US3 scenario 1).

    Scans are inserted straight into Checkout's scan table so the test stays fast; the
    Analytics pipeline pulls them from Checkout's scan log exactly as it does live. Windows
    end on slide boundaries (005 research R5), so 1500 scans give (500, 1500].
    """
    from analytics_service import popular_items as analytics

    # 1500 line items: the first 600 are SKU-000001, the rest SKU-000002.
    s = checkout_session
    tx_id = await s.scalar(
        text("INSERT INTO transaction (station_id) VALUES ('bulk') RETURNING id")
    )
    await s.execute(
        text(
            "INSERT INTO transaction_item (transaction_id, sku, unit_price_cents)"
            " SELECT :tx, CASE WHEN g <= 600 THEN 'SKU-000001' ELSE 'SKU-000002' END, 100"
            "   FROM generate_series(1, 1500) AS g"
        ),
        {"tx": tx_id},
    )
    await s.commit()

    await poll_analytics()
    await _wait_published(1500)
    body = await analytics.read(limit=10)

    # The window covers the most recent 1000 scans, so only 100 of the 600
    # SKU-000001 scans remain inside it.
    assert body["windowEnd"] - body["windowStart"] == 1000
    counts = {i["sku"]: i["scanCount"] for i in body["items"]}
    assert counts["SKU-000002"] == 900
    assert counts["SKU-000001"] == 100


async def test_restarted_analytics_republishes_the_same_window(client, analytics, poll_analytics):
    """An Analytics that lost its memory rebuilds the same window from Checkout's scans."""
    from analytics_service import popular_items as analytics_read

    await _scan_many(client, {"SKU-000001": 500})
    await poll_analytics()
    await _wait_published(500)
    first = await analytics_read.read(limit=10)

    await analytics.restart()  # memory is lost; Checkout's scan log is not
    await _wait_published(500)
    second = await analytics_read.read(limit=10)

    assert first["items"] == second["items"]
    assert first["windowEnd"] == second["windowEnd"]


async def test_only_the_latest_ranking_is_kept(client, analytics, poll_analytics):
    """Each slide replaces the published ranking; nothing accumulates."""
    await _scan_many(client, {"SKU-000001": 500})
    await poll_analytics()
    await _wait_published(500)
    assert (state.latest.window_start, state.latest.window_end) == (0, 500)

    await _scan_many(client, {"SKU-000002": 500})
    await poll_analytics()
    await _wait_published(1000)
    assert (state.latest.window_start, state.latest.window_end) == (0, 1000)
