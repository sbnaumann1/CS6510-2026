"""Analytics Service: understanding customer buying behavior.

Serves the popular-items window and runs the one persistent streaming pipeline that
keeps it up to date (specs/007-microservices-architecture/research.md R5). It has no
database: it consumes Checkout's scan events from Kafka (spec 008) and keeps the window in
memory, rebuilding it from the topic's retained tail after a restart. Nothing in Checkout
knows this service exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from analytics_service import api, catalog_snapshot, eventbus, internal_reports, scan_source
from analytics_service.config import settings
from analytics_service.supervisor import PipelineSupervisor
from checkout_web.app import create_service_app
from checkout_web.lifecycle import cancel_and_wait
from checkout_web.bus import Bus, KafkaBus

log = logging.getLogger("analytics")


def _configure_logging() -> None:
    """Pipeline lifecycle lines at INFO, independent of uvicorn's --log-level
    warning (the quickstart and the pre-push review grep these)."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


async def start_background(bus: Bus) -> list[asyncio.Task]:
    """Start Analytics' event work on `bus`: the catalog snapshot and the scan consumer.
    (The pipeline supervisor is started by the lifespan; tests run their own.) Lifespans do
    not run in-process under test, so the test fixtures call this with a MemoryBus."""
    await bus.start()
    eventbus.set_bus(bus)
    catalog_snapshot.reset()
    scan_source.reset()
    return [
        asyncio.create_task(catalog_snapshot.get_snapshot().run(), name="catalog-snapshot"),
        asyncio.create_task(scan_source.default_source().run(), name="scan-consumer"),
    ]


async def stop_background(tasks: list[asyncio.Task]) -> None:
    await cancel_and_wait(tasks)


def readiness() -> dict:
    """Checked by /healthz: the event path must work before traffic arrives."""
    bus = eventbus._bus
    return {
        "event bus connected": lambda: bus is not None and bus.ready(),
        "scan window rebuilt": lambda: scan_source.default_source().caught_up,
        "catalog snapshot loaded": lambda: catalog_snapshot.get_snapshot().ready,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    bus = KafkaBus(settings.kafka_bootstrap, client_id="analytics")
    event_tasks = await start_background(bus)
    supervisor = PipelineSupervisor()
    task = asyncio.create_task(supervisor.run(), name="pipeline-supervisor")
    try:
        yield
    finally:
        await supervisor.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await stop_background(event_tasks)
        await bus.stop()


async def probe() -> None:
    """Healthy as soon as the process is up; the event path is reported by `readiness`."""


app = create_service_app(
    "analytics", [api.router, internal_reports.router], probe, lifespan, readiness=readiness
)
