"""Every cross-service step is bounded (FR-017, SC-005): a dependency that never answers
produces a failure and a rollback, not a hang."""

from __future__ import annotations

import time

from tests.failure._helpers import assert_consistent, basket, complete, stock_of


async def test_an_inventory_that_never_answers_is_a_503_within_the_saga_budget(client, mesh):
    tx = await basket(client, {"SKU-000001": 2})
    mesh.fail("inventory", "/internal/reservations", times=99, mode="timeout", method="POST")

    started = time.monotonic()
    r = await complete(client, tx)
    elapsed = time.monotonic() - started

    assert r.status_code == 503 and r.json()["error"] == "INTERNAL_ERROR"
    assert elapsed < 7, "bounded: the reserve budget plus one release attempt"
    assert mesh.calls_to("inventory", "/internal/reservations", "POST") == 3, "bounded retries"
    assert await stock_of("SKU-000001") == 100
    await assert_consistent()


async def test_retries_never_exceed_the_configured_number(client, mesh):
    from checkout_service import inventory_client

    tx = await basket(client, {"SKU-000001": 1})
    mesh.fail("inventory", "/internal/reservations", times=99, mode="error", method="POST")
    r = await complete(client, tx)
    assert r.status_code == 503
    # Retries are for idempotent calls only, and never more than the configured number.
    assert mesh.calls_to("inventory", "/internal/reservations", "POST") <= 1 + inventory_client.RETRIES


async def test_a_slow_analytics_never_delays_a_scan(client, mesh, analytics):
    mesh.fail("checkout", "/internal/scans", times=10_000, mode="delay", delay=2.0)
    started = time.monotonic()
    tx = await basket(client, {"SKU-000001": 10})
    assert (await complete(client, tx)).status_code == 200
    assert time.monotonic() - started < 1.5, "Checkout is not on Analytics' call path"


async def test_catalog_down_after_the_snapshot_is_warm_does_not_stop_scans(client, mesh):
    await basket(client, {"SKU-000001": 1})  # warms Checkout's catalog snapshot
    mesh.fail("catalog", "/items", times=10_000, mode="error")
    tx = await basket(client, {"SKU-000001": 2, "SKU-000002": 1})
    r = await complete(client, tx)
    assert r.status_code == 200
    assert mesh.calls_to("catalog", "/items") == 1, "one fetch warmed it; none after"
