"""GET /analytics/popular-items (FR-009)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._request import positive_int_param
from app.db import get_session
from app.analytics import popular_items as svc

router = APIRouter(tags=["analytics"])


@router.get("/analytics/popular-items")
async def popular_items(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    limit = positive_int_param(request, "limit", 10)
    return ORJSONResponse(content=await svc.read(session, limit))
