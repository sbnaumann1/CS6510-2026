"""GET /inventory/low-stock (FR-007)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.errors import INVALID_REQUEST, ApiError
from app.services import inventory as svc

router = APIRouter(tags=["inventory"])


def positive_int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(
            INVALID_REQUEST, f"'{name}' must be a positive integer."
        ) from None
    if value <= 0:
        raise ApiError(INVALID_REQUEST, f"'{name}' must be a positive integer.")
    return value


@router.get("/inventory/low-stock")
async def low_stock(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    threshold = positive_int_param(request, "threshold", settings.low_stock_threshold)
    return ORJSONResponse(content=await svc.low_stock(session, threshold))
