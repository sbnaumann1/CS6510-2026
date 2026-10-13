"""Checkout Service: running a basket from start to payment.

Owns transactions, scans, stock decrements and low-stock alert emission, plus
the abandoned-transaction sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from checkout_data import SessionLocal, catalog, dispose_engine, ping
from checkout_data.config import settings
from checkout_service import api, sweeper
from checkout_web.app import create_service_app

log = logging.getLogger("checkout")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SessionLocal() as session:
        n = await catalog.load(session)
    if n == 0:
        log.warning("catalog is empty — run the seed (python -m checkout_data.seed)")
    else:
        log.info("catalog cache loaded: %d items", n)

    sweep = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        sweep.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep
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


async def probe() -> None:
    await ping()
    if catalog.size() == 0:
        raise RuntimeError("catalog cache is empty")


app = create_service_app("checkout", [api.router], probe, lifespan)
