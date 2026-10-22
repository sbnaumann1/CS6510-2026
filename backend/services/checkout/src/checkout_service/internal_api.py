"""Checkout's internal scan log, read by Analytics over the mesh
(contracts/internal-apis.md). The gateway never routes /internal/.

A primary-key range read over Checkout's own scan table. Checkout keeps no per-caller
state, so Analytics can restart, re-read or re-bootstrap at any time.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from checkout_service import repo
from checkout_service.db import get_autocommit_session
from checkout_web.errors import INVALID_REQUEST, ApiError

router = APIRouter(tags=["internal"])

MAX_LIMIT = 1000


def _int_param(request: Request, name: str, default: int | None, *, low: int, high: int | None) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        if default is None:
            raise ApiError(INVALID_REQUEST, f"'{name}' is required.")
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(INVALID_REQUEST, f"'{name}' must be an integer.") from None
    if value < low or (high is not None and value > high):
        raise ApiError(INVALID_REQUEST, f"'{name}' is out of range.")
    return value


@router.get("/internal/scans")
async def scans(
    request: Request, session: AsyncSession = Depends(get_autocommit_session)
) -> Response:
    after = _int_param(request, "after", None, low=0, high=None)
    limit = _int_param(request, "limit", MAX_LIMIT, low=0, high=MAX_LIMIT)
    rows = await repo.scans_after(session, after, limit) if limit else []
    max_id = await repo.max_scan_id(session)
    return ORJSONResponse(
        content={
            "scans": [{"scanId": r.id, "sku": r.sku} for r in rows],
            "maxScanId": max_id,
        }
    )
