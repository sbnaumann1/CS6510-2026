"""Catalog Service: what can be sold, and at what price.

Its private store is a read-only file; there is no database (spec 007 research R8).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from catalog_service import api, publisher, store
from catalog_service.config import settings
from checkout_web.app import create_service_app
from checkout_web.lifecycle import cancel_and_wait
from checkout_web.bus import Bus, KafkaBus

log = logging.getLogger("checkout")


def load() -> int:
    """Load the file and pre-render the /items body. Raises if the file is invalid."""
    n = store.load(settings.catalog_file)
    api.set_body(api.render(store.rows()))
    return n


async def start_background(bus: Bus) -> list[asyncio.Task]:
    """Start Catalog's event work on `bus`: publish the catalog (retrying until the broker
    accepts it). Lifespans do not run in-process under test, so the test fixtures call
    this directly with a MemoryBus."""
    await bus.start()
    return [asyncio.create_task(publisher.publish_catalog(bus), name="catalog-publisher")]


async def stop_background(tasks: list[asyncio.Task]) -> None:
    await cancel_and_wait(tasks)


@asynccontextmanager
async def lifespan(app: FastAPI):
    n = load()
    if n == 0:
        log.warning("catalog is empty — run the seed (python -m catalog_service.seed)")
    bus = KafkaBus(settings.kafka_bootstrap, client_id="catalog")
    tasks = await start_background(bus)
    try:
        yield
    finally:
        await stop_background(tasks)
        await bus.stop()


async def probe() -> None:
    if store.size() == 0:
        raise RuntimeError("catalog is empty")


app = create_service_app("catalog", [api.router], probe, lifespan)
