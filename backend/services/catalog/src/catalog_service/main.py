"""Catalog Service: what can be sold, and at what price."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from catalog_service import api
from checkout_data import SessionLocal, catalog, dispose_engine, ping
from checkout_web.app import create_service_app

log = logging.getLogger("checkout")


async def load() -> int:
    """Load the catalog cache and pre-render the /items body."""
    async with SessionLocal() as session:
        n = await catalog.load(session)
    api.set_body(api.render(catalog.rows()))
    return n


@asynccontextmanager
async def lifespan(app: FastAPI):
    n = await load()
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


app = create_service_app("catalog", [api.router], probe, lifespan)
