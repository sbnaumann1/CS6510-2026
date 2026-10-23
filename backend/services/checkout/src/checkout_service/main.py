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

from checkout_service import api, catalog_client, internal_api, internal_reports, recovery, sweeper
from checkout_service.config import settings
from checkout_service.db import dispose_engine, ping
from checkout_web.app import create_service_app

log = logging.getLogger("checkout")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open one connection before traffic: SQLAlchemy runs a pool's first-connect hook under
    # a threading lock, which deadlocks the event loop if several requests race to open the
    # pool's first connections.
    try:
        await ping()
    except Exception:
        log.warning("database not reachable yet; will retry on first use")
    tasks = [
        asyncio.create_task(_sweep_loop(), name="sweeper"),
        asyncio.create_task(recovery.recovery_loop(), name="recovery"),
        asyncio.create_task(catalog_client.warm_forever(), name="catalog-warm-up"),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
    [api.router, internal_api.router, internal_reports.router],
    ping,
    lifespan,
)
