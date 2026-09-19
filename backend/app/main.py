"""Application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from app import background, catalog_cache
from app.api import analytics, catalog, inventory, transactions
from app.config import settings
from app.db import SessionLocal, dispose_engine
from app.errors import register_error_handlers

log = logging.getLogger("checkout")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SessionLocal() as session:
        n = await catalog_cache.load(session)
    if n == 0:
        log.warning("catalog is empty — run: uv run python scripts/seed.py --reset")
    else:
        log.info("catalog cache loaded: %d items", n)

    sweeper = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
        await dispose_engine()


async def _sweep_loop() -> None:
    """Cancel abandoned transactions periodically. Idempotent, so it is safe to
    run in every worker."""
    interval = max(30, settings.tx_abandon_minutes * 60 // 2)
    while True:
        await asyncio.sleep(interval)
        try:
            n = await background.sweep_abandoned()
            if n:
                log.info("swept %d abandoned transaction(s)", n)
        except Exception:
            log.exception("sweeper failed")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Supermarket Self-Checkout API",
        version="1.0.0",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.include_router(catalog.router)
    app.include_router(transactions.router)
    app.include_router(inventory.router)
    app.include_router(analytics.router)
    return app


app = create_app()
