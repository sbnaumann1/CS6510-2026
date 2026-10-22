"""Analytics Service: understanding customer buying behavior.

Serves the popular-items window and runs the one persistent streaming pipeline that
keeps it up to date (specs/007-microservices-architecture/research.md R5). It has no
database: the pipeline pulls scans from Checkout's scan log and keeps the window in
memory, rebuilding it from Checkout after a restart. Nothing in Checkout knows this
service exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from analytics_service import api, internal_reports
from analytics_service.supervisor import PipelineSupervisor
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
    supervisor = PipelineSupervisor()
    task = asyncio.create_task(supervisor.run(), name="pipeline-supervisor")
    try:
        yield
    finally:
        await supervisor.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def probe() -> None:
    """Healthy as soon as the process is up. Analytics waits for Checkout on its own
    and answers with the empty window meanwhile, so its health does not depend on it."""


app = create_service_app("analytics", [api.router, internal_reports.router], probe, lifespan)
