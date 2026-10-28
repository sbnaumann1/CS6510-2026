"""Inventory Service: stock levels, reservations and low-stock alerts.

Owns the stock table (spec 007: stock moved here from Checkout). Checkout reduces stock
only by asking over events (spec 008): Inventory consumes reservation and release requests
and answers with outcome events. Inventory never reads Checkout's data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from checkout_web.app import create_service_app
from checkout_web.lifecycle import cancel_and_wait
from checkout_web.bus import Bus, KafkaBus
from inventory_service import api, catalog_snapshot, eventbus, handlers, internal_api, internal_reports, outbox
from inventory_service.config import settings
from inventory_service.db import dispose_engine, ping


log = logging.getLogger("inventory")


async def start_background(bus: Bus, *, outbox_sweeper: bool = True) -> list[asyncio.Task]:
    """Start Inventory's event work on `bus`: the catalog snapshot, the consumer of
    reservation and release requests, and the outbox sweeper. Lifespans do not run
    in-process under test, so the test fixtures call this directly with a MemoryBus."""
    await bus.start()
    eventbus.set_bus(bus)
    catalog_snapshot.reset()
    tasks = [
        asyncio.create_task(catalog_snapshot.get_snapshot().run(), name="catalog-snapshot"),
        asyncio.create_task(handlers.run(bus), name="completion-requests"),
    ]
    if outbox_sweeper:
        tasks.append(asyncio.create_task(outbox.sweeper_loop(), name="outbox-sweeper"))
    return tasks


async def stop_background(tasks: list[asyncio.Task]) -> None:
    await cancel_and_wait(tasks)


def readiness() -> dict:
    """Checked by /healthz: the event path must work before traffic arrives."""
    bus = eventbus._bus
    return {
        "event bus connected": lambda: bus is not None and bus.ready(),
        "request consumer assigned": handlers.ready,
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
    bus = KafkaBus(settings.kafka_bootstrap, client_id="inventory")
    tasks = await start_background(bus)
    try:
        yield
    finally:
        await stop_background(tasks)
        await bus.stop()
        await dispose_engine()


app = create_service_app(
    "inventory",
    [api.router, internal_api.router, internal_reports.router],
    ping,
    lifespan,
    readiness=readiness,
)
