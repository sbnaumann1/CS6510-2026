"""Read-only stock report for the cross-store verification tool (research R10)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_service import repo
from inventory_service.db import get_session

router = APIRouter(tags=["internal"])


@router.get("/internal/reports/stock")
async def stock(session: AsyncSession = Depends(get_session)) -> Response:
    rows = await repo.stock_report(session)
    adjustments = await repo.adjustments(session)
    return ORJSONResponse(
        content={
            "unpublishedOutbox": await repo.unpublished_count(session),
            "stock": [
                {"sku": r.sku, "initial": r.initial_stock, "current": r.current_stock}
                for r in rows
            ],
            "adjustments": {
                "applied": [f"tx-{a.transaction_id}" for a in adjustments if a.state == "APPLIED"],
                "released": [f"tx-{a.transaction_id}" for a in adjustments if a.state == "RELEASED"],
            },
        }
    )
