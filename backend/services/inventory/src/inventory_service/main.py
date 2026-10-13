"""Inventory Service: reporting stock levels that need attention.

Read-only over the stock and alert rows Checkout writes; never calls Checkout.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from checkout_data import dispose_engine, ping
from checkout_web.app import create_service_app
from inventory_service import api


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await dispose_engine()


app = create_service_app("inventory", [api.router], ping, lifespan)
