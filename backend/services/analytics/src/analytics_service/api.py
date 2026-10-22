"""GET /analytics/popular-items (FR-009)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import ORJSONResponse

from analytics_service import popular_items as svc
from checkout_web.request import positive_int_param

router = APIRouter(tags=["analytics"])


@router.get("/analytics/popular-items")
async def popular_items(request: Request) -> Response:
    limit = positive_int_param(request, "limit", 10)
    return ORJSONResponse(content=await svc.read(limit))
