"""Inventory Service: stock levels, reservations and low-stock alerts.

Owns the stock table (spec 007: stock moved here from Checkout). Checkout reduces stock
only by calling the reservation routes; Inventory never reads Checkout's data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from checkout_web.app import create_service_app
from inventory_service import api, catalog_client, internal_api, internal_reports
from inventory_service.db import dispose_engine, ping


log = logging.getLogger("inventory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open one connection before traffic: SQLAlchemy runs a pool's first-connect hook under
    # a threading lock, which deadlocks the event loop if several requests race to open the
    # pool's first connections.
    try:
        await ping()
    except Exception:
        log.warning("database not reachable yet; will retry on first use")
    warm = asyncio.create_task(catalog_client.warm_forever(), name="catalog-warm-up")
    try:
        yield
    finally:
        warm.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warm
        await dispose_engine()


app = create_service_app(
    "inventory",
    [api.router, internal_api.router, internal_reports.router],
    ping,
    lifespan,
)
