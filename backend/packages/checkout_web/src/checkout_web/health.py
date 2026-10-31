"""GET /healthz — readiness for compose health checks (005 research R13).

Not part of the frozen contract and not routed by the gateway. The probe is
supplied by each service, so this module stays free of database access.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Mapping

from fastapi import APIRouter
from fastapi.responses import ORJSONResponse

from checkout_web.errors import INTERNAL_ERROR

log = logging.getLogger("checkout")


def health_router(
    service: str,
    probe: Callable[[], Awaitable[None]],
    readiness: Mapping[str, Callable[[], bool]] | Callable[[], Mapping[str, Callable[[], bool]]] | None = None,
) -> APIRouter:
    """`readiness` maps a name to a cheap synchronous check (or is a function returning that
    mapping, for checks that exist only after startup) (broker connected, consumer
    positioned, catalog snapshot loaded). /healthz is 200 only when the probe and every
    check pass, so `docker compose --wait` blocks until the event path works (spec 008)."""
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
        for check, ok in (readiness() if callable(readiness) else (readiness or {})).items():
            if not ok():
                log.warning("%s not ready: %s", service, check)
                return ORJSONResponse(
                    status_code=503,
                    content={"error": INTERNAL_ERROR[0], "message": f"{service} is not ready: {check}."},
                )
        return ORJSONResponse(content={"status": "ok", "service": service})

    return router
