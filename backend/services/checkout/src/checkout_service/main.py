"""Checkout Service: running a basket from start to payment.

Owns transactions, scans and the completion saga, plus the abandoned-transaction sweep
and the recovery loop. Stock is Inventory's: completing a basket reserves it there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from checkout_service import (
    api,
    catalog_snapshot,
    eventbus,
    internal_reports,
    outbox,
    recovery,
    relay,
    stock_events,
    sweeper,
)
from checkout_service.config import settings
from checkout_service.db import dispose_engine, ping
from checkout_web.app import create_service_app
from checkout_web.lifecycle import cancel_and_wait
from checkout_web.bus import Bus, KafkaBus

log = logging.getLogger("checkout")


async def start_background(bus: Bus, *, outbox_sweeper: bool = True, scan_relay: bool = True) -> list[asyncio.Task]:
    """Start Checkout's event work on `bus`: the catalog snapshot, the reply consumer for
    Inventory's outcomes, the outbox sweeper and the scan relay. Lifespans do not run
    in-process under test, so the test fixtures call this directly with a MemoryBus."""
    await bus.start()
    eventbus.set_bus(bus)
    catalog_snapshot.reset()
    stock_events.reset()
    relay.reset()
    tasks = [
        asyncio.create_task(catalog_snapshot.get_snapshot().run(), name="catalog-snapshot"),
        asyncio.create_task(stock_events.get_client().run(), name="stock-replies"),
    ]
    if outbox_sweeper:
        tasks.append(asyncio.create_task(outbox.sweeper_loop(), name="outbox-sweeper"))
    if scan_relay:
        tasks.append(asyncio.create_task(relay.get_relay().run(), name="scan-relay"))
    return tasks


async def stop_background(tasks: list[asyncio.Task]) -> None:
    await cancel_and_wait(tasks)


def readiness() -> dict:
    """Checked by /healthz: the event path must work before traffic arrives."""
    bus = eventbus._bus
    return {
        "event bus connected": lambda: bus is not None and bus.ready(),
        "stock reply consumer positioned": lambda: stock_events.get_client().ready,
        "catalog snapshot loaded": lambda: catalog_snapshot.get_snapshot().ready,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open one connection before traffic: SQLAlchemy runs a pool's first-connect hook under
    # a threading lock, which deadlocks the event loop if several requests race to open the
    # pool's first connections.
    try:
        await ping()
    except Exception:
        log.warning("database not reachable yet; will retry on first use")
    bus = KafkaBus(settings.kafka_bootstrap, client_id="checkout")
    events_tasks = await start_background(bus)
    tasks = [
        asyncio.create_task(_sweep_loop(), name="sweeper"),
        asyncio.create_task(recovery.recovery_loop(), name="recovery"),
    ]
    try:
        yield
    finally:
        await stop_background(tasks + events_tasks)
        await bus.stop()
        await dispose_engine()


async def _sweep_loop() -> None:
    """Cancel abandoned transactions periodically. Idempotent, so it is safe to
    run in every worker and every Checkout replica."""
    interval = max(30, settings.tx_abandon_minutes * 60 // 2)
    while True:
        await asyncio.sleep(interval)
        try:
            n = await sweeper.sweep_abandoned()
            if n:
                log.info("swept %d abandoned transaction(s)", n)
        except Exception:
            log.exception("sweeper failed")


app = create_service_app(
    "checkout",
    [api.router, internal_reports.router],
    ping,
    lifespan,
    readiness=readiness,
)
