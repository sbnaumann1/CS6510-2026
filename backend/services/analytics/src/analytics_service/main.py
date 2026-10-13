"""Analytics Service: understanding customer buying behavior.

Serves the popular-items window and runs the one persistent streaming pipeline
that keeps it up to date (specs/005-service-architecture/research.md R5). The
pipeline learns about scans only by tailing the shared scan record; nothing in
Checkout knows this service exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from analytics_service import api
from analytics_service.supervisor import PipelineSupervisor
from checkout_data import SessionLocal, catalog, dispose_engine, ping
from checkout_web.app import create_service_app

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    # Item names in the ranking come from this process's own catalog copy.
    async with SessionLocal() as session:
        n = await catalog.load(session)
    if n == 0:
        log.warning("catalog is empty — run the seed (python -m checkout_data.seed)")

    supervisor = PipelineSupervisor()
    task = asyncio.create_task(supervisor.run(), name="pipeline-supervisor")
    try:
        yield
    finally:
        await supervisor.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await dispose_engine()


async def probe() -> None:
    await ping()
    if catalog.size() == 0:
        raise RuntimeError("catalog cache is empty")


app = create_service_app("analytics", [api.router], probe, lifespan)
