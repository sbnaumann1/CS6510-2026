"""Application factory shared by the four domain services.

Every service gets the same contract conventions: ORJSON responses, the
error-catalog handlers, and a /healthz endpoint backed by its own probe.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import APIRouter, FastAPI
from fastapi.responses import ORJSONResponse

from checkout_web.errors import register_error_handlers
from checkout_web.health import health_router
from checkout_web.request import RequestIdMiddleware


def create_service_app(
    name: str,
    routers: list[APIRouter],
    probe: Callable[[], Awaitable[None]],
    lifespan=None,
) -> FastAPI:
    app = FastAPI(
        title=f"Supermarket Self-Checkout API — {name}",
        version="1.0.0",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    for router in routers:
        app.include_router(router)
    app.include_router(health_router(name, probe))
    return app
