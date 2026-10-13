"""GET /healthz — readiness for compose health checks (005 research R13).

Not part of the frozen contract and not routed by the gateway. The probe is
supplied by each service, so this module stays free of database access.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import ORJSONResponse

from checkout_web.errors import INTERNAL_ERROR

log = logging.getLogger("checkout")


def health_router(service: str, probe: Callable[[], Awaitable[None]]) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> ORJSONResponse:
        try:
            await probe()
        except Exception as exc:
            log.warning("%s not ready: %s", service, exc)
            return ORJSONResponse(
                status_code=503,
                content={"error": INTERNAL_ERROR[0], "message": f"{service} is not ready."},
            )
        return ORJSONResponse(content={"status": "ok", "service": service})

    return router
