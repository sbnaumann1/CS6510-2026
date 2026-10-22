"""Inventory Service: stock levels, reservations and low-stock alerts.

Owns the stock table (spec 007: stock moved here from Checkout). Checkout reduces stock
only by calling the reservation routes; Inventory never reads Checkout's data.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from checkout_web.app import create_service_app
from inventory_service import api, internal_api, internal_reports
from inventory_service.db import dispose_engine, ping


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await dispose_engine()


app = create_service_app(
    "inventory",
    [api.router, internal_api.router, internal_reports.router],
    ping,
    lifespan,
)
