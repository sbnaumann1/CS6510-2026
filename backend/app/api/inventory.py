"""GET /inventory/low-stock (FR-007)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._request import positive_int_param
from app.config import settings
from app.db import get_session
from app.analytics import low_stock as svc

router = APIRouter(tags=["inventory"])


@router.get("/inventory/low-stock")
async def low_stock(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    threshold = positive_int_param(request, "threshold", settings.low_stock_threshold)
    return ORJSONResponse(content=await svc.read(session, threshold))
