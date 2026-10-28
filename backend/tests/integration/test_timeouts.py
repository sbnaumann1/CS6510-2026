"""Every cross-service step is bounded (FR-014, SC-009): a dependency that never answers
produces a failure and a rollback, not a hang. Since spec 008 the dependency is the event
path: a request that is never answered, a broker that refuses publishes, a consumer that is
slow.
"""

from __future__ import annotations

import time

from checkout_web import events as ev
from checkout_service.config import settings
from tests.failure._helpers import (
    RESERVATION_REQUESTS,
    assert_consistent,
    basket,
    complete,
    of_type,
    stock_of,
)


async def test_an_inventory_that_never_answers_is_a_503_within_the_saga_budget(client, stack):
    tx = await basket(client, {"SKU-000001": 2})
    stack.bus.drop(RESERVATION_REQUESTS, times=99)

    started = time.monotonic()
    r = await complete(client, tx)
    elapsed = time.monotonic() - started

    assert r.status_code == 503 and r.json()["error"] == "INTERNAL_ERROR"
    budget = settings.reserve_budget_s + settings.release_wait_s
    assert elapsed < budget + 1.5, "bounded: the reserve budget plus one release wait"
    assert stack.bus.attempts_of(ev.STOCK_RESERVATION_REQUESTED) == 3, "the request, then two re-publishes"
    assert await stock_of("SKU-000001") == 100
    await assert_consistent()


async def test_republishing_never_exceeds_the_configured_number(client, stack):
    tx = await basket(client, {"SKU-000001": 1})
    stack.bus.fail_publish(times=99, predicate=RESERVATION_REQUESTS)  # the broker refuses every request
    r = await complete(client, tx)
    assert r.status_code == 503
    # One attempt plus two re-publishes, and no more however long the broker stays down.
    assert stack.bus.attempts_of(ev.STOCK_RESERVATION_REQUESTED) <= 3


async def test_a_slow_analytics_never_delays_a_scan(client, stack, analytics):
    stack.bus.delay(of_type(ev.SCAN_RECORDED), 2.0, times=10_000)
    started = time.monotonic()
    tx = await basket(client, {"SKU-000001": 10})
    assert (await complete(client, tx)).status_code == 200
    assert time.monotonic() - started < 1.5, "Checkout is not on Analytics' path"


async def test_catalog_down_after_the_snapshot_is_loaded_does_not_stop_scans(client, stack):
    """Catalog's events were read once at startup; after that Catalog is not needed."""
    await stack.stop_service("catalog")
    tx = await basket(client, {"SKU-000001": 2, "SKU-000002": 1})
    assert (await complete(client, tx)).status_code == 200


async def test_catalog_is_published_again_until_the_broker_accepts_it(reset_db, bus):
    """Catalog retries its publication until the broker takes all of it, so services that
    start meanwhile still become ready."""
    import asyncio

    from catalog_service import publisher, store
    from catalog_service.main import load
    from checkout_web.snapshot import TopicSnapshot

    load()

    bus.fail_publish(times=2, predicate=of_type(ev.ITEM_PUBLISHED, ev.CATALOG_PUBLISHED))
    snap = TopicSnapshot(bus, name="t", parse=lambda sku, d: d["name"])
    reader = asyncio.create_task(snap.run())
    n = await publisher.publish_catalog(bus, retry_s=0.01)
    assert n == store.size() > 0
    assert await snap.wait_ready(2.0)
    reader.cancel()
