"""Catalog Service: what can be sold, and at what price.

Its private store is a read-only file; there is no database (spec 007 research R8).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from catalog_service import api, store
from catalog_service.config import settings
from checkout_web.app import create_service_app

log = logging.getLogger("checkout")


def load() -> int:
    """Load the file and pre-render the /items body. Raises if the file is invalid."""
    n = store.load(settings.catalog_file)
    api.set_body(api.render(store.rows()))
    return n


@asynccontextmanager
async def lifespan(app: FastAPI):
    n = load()
    if n == 0:
        log.warning("catalog is empty — run the seed (python -m catalog_service.seed)")
    yield


async def probe() -> None:
    if store.size() == 0:
        raise RuntimeError("catalog is empty")


app = create_service_app("catalog", [api.router], probe, lifespan)
