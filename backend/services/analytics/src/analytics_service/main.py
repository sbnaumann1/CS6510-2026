"""Analytics Service: understanding customer buying behavior.

Serves the popular-items window. The persistent streaming pipeline that keeps
it up to date is started here once it exists (US3); until then the read path
answers on demand.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from analytics_service import api
from checkout_data import SessionLocal, catalog, dispose_engine, ping
from checkout_web.app import create_service_app

log = logging.getLogger("analytics")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Item names in the ranking come from this process's own catalog copy.
    async with SessionLocal() as session:
        n = await catalog.load(session)
    if n == 0:
        log.warning("catalog is empty — run the seed (python -m checkout_data.seed)")
    try:
        yield
    finally:
        await dispose_engine()


async def probe() -> None:
    await ping()
    if catalog.size() == 0:
        raise RuntimeError("catalog cache is empty")


app = create_service_app("analytics", [api.router], probe, lifespan)
