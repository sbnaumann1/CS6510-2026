"""The cross-store verification tool over the services' report endpoints
(research R10): it opens no database, and it catches drift between the stores."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.failure._helpers import basket, complete

TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "verify_invariant.py"
_spec = importlib.util.spec_from_file_location("verify_invariant_tool", TOOL)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


async def reports(stack):
    out = {}
    for key, host, path in (
        ("sold", "checkout", "/internal/reports/sold-by-sku"),
        ("scans", "checkout", "/internal/reports/scan-count"),
        ("stock", "inventory", "/internal/reports/stock"),
        ("cursor", "analytics", "/internal/reports/cursor"),
    ):
        async with AsyncClient(transport=stack, base_url=f"http://{host}:8000") as c:
            r = await c.get(path)
            assert r.status_code == 200, (path, r.text)
            out[key] = r.json()
    return out["sold"], out["scans"], out["stock"], out["cursor"]


async def test_passes_after_normal_traffic(client, stack, poll_analytics):
    for _ in range(3):
        tx = await basket(client, {"SKU-000001": 2, "SKU-000004": 1})
        assert (await complete(client, tx)).status_code == 200
    await basket(client, {"SKU-000002": 2})  # scanned, never completed
    await poll_analytics()

    ok, lines = verify.check(*await reports(stack))

    assert ok, "\n".join(lines)
    assert "PASS" in lines[-1]


async def test_fails_when_stock_drifts_from_what_was_sold(client, stack, poll_analytics, inventory_session):
    tx = await basket(client, {"SKU-000001": 2})
    assert (await complete(client, tx)).status_code == 200
    await poll_analytics()
    await inventory_session.execute(
        text("UPDATE inventory_stock SET current_stock = current_stock - 5 WHERE sku = 'SKU-000001'")
    )
    await inventory_session.commit()

    ok, lines = verify.check(*await reports(stack))

    assert not ok
    assert any("SKU-000001" in line and "drift=+5" in line for line in lines)


async def test_fails_when_a_transaction_is_stuck_completing(client, stack, poll_analytics):
    from checkout_service import repo
    from checkout_service.db import SessionLocal

    tx = await basket(client, {"SKU-000001": 1})
    async with SessionLocal() as s:
        async with s.begin():
            await repo.mark_completing(s, int(tx.removeprefix("tx-")), 85)
    await poll_analytics()

    ok, lines = verify.check(*await reports(stack))

    assert not ok
    assert any("COMPLETING" in line for line in lines)


async def test_fails_when_analytics_has_not_read_every_scan(client, stack, analytics):
    await basket(client, {"SKU-000001": 3})
    sold, scans, stock, _ = await reports(stack)

    ok, lines = verify.check(sold, scans, stock, {"lastScanId": 1, "windowEnd": 0})

    assert not ok
    assert any("Analytics has read to scan 1" in line for line in lines)


@pytest.mark.parametrize("path", ["/internal/reports/stock", "/internal/reports/cursor"])
async def test_reports_are_not_reachable_through_the_gateway(client, path):
    assert (await client.get(path)).status_code == 404


async def test_fails_when_an_outbox_row_is_still_unpublished(client, stack, poll_analytics):
    await basket(client, {"SKU-000001": 2})
    await poll_analytics()
    sold, scans, stock, cursor = await reports(stack)
    assert verify.check(sold, scans, stock, cursor)[0], "a healthy system passes"

    ok, lines = verify.check(sold, {**scans, "unpublishedOutbox": 3}, stock, cursor)
    assert not ok and any("Checkout has 3 outbox row(s)" in line for line in lines)
    ok, lines = verify.check(sold, scans, {**stock, "unpublishedOutbox": 1}, cursor)
    assert not ok and any("Inventory has 1 outbox row(s)" in line for line in lines)


async def test_fails_when_the_relay_has_not_published_every_scan(client, stack, poll_analytics):
    await basket(client, {"SKU-000001": 3})
    await poll_analytics()
    sold, scans, stock, cursor = await reports(stack)
    assert scans["relayPosition"] == scans["maxScanId"] == 3

    ok, lines = verify.check(sold, {**scans, "relayPosition": 1}, stock, cursor)
    assert not ok and any("scan relay has published to scan 1" in line for line in lines)


async def test_the_reports_show_a_stuck_outbox_row(client, stack):
    from sqlalchemy import text as sql

    from checkout_service import repo, stock_events
    from checkout_service.db import SessionLocal
    from checkout_web import events as ev

    async with SessionLocal() as s:
        async with s.begin():
            await repo.insert_outbox(s, ev.CHECKOUT_COMPLETIONS, "tx-1", stock_events.release_request("tx-1", 1))
            await s.execute(sql("UPDATE outbox SET created_at = now() - interval '1 minute'"))
    _, scans, _, _ = await reports(stack)
    assert scans["unpublishedOutbox"] == 1
