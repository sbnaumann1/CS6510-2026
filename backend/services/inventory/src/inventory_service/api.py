"""GET /inventory/low-stock (FR-007)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from checkout_web.request import positive_int_param
from inventory_service import low_stock as svc
from inventory_service.config import settings
from inventory_service.db import get_session

router = APIRouter(tags=["inventory"])


@router.get("/inventory/low-stock")
async def low_stock(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    threshold = positive_int_param(request, "threshold", settings.low_stock_threshold)
    return ORJSONResponse(content=await svc.read(session, threshold))
