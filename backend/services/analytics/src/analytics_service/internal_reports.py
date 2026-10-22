"""Read-only report for the cross-store verification tool (research R10).

Reports how far this process has read Checkout's scan log, from memory.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import ORJSONResponse

from analytics_service import state

router = APIRouter(tags=["internal"])


@router.get("/internal/reports/cursor")
async def cursor() -> Response:
    latest = state.latest
    return ORJSONResponse(
        content={
            "lastScanId": state.cursor,
            "windowEnd": latest.window_end if latest is not None else 0,
        }
    )
