"""Read-only reports for the cross-store verification tool (research R10).

The checker is not a back door into any database: it asks each service to describe its own
data over the mesh. The gateway never routes /internal/.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from checkout_service import repo
from checkout_service.db import get_autocommit_session

router = APIRouter(tags=["internal"])


@router.get("/internal/reports/sold-by-sku")
async def sold_by_sku(session: AsyncSession = Depends(get_autocommit_session)) -> Response:
    sold = {r.sku: r.sold for r in await repo.sold_by_sku(session)}
    return ORJSONResponse(
        content={
            "sold": sold,
            "completing": await repo.completing_count(session),
            "openCompletions": await repo.open_completions(session),
            "completedTransactions": [f"tx-{i}" for i in await repo.completed_ids(session)],
        }
    )


@router.get("/internal/reports/scan-count")
async def scan_count(session: AsyncSession = Depends(get_autocommit_session)) -> Response:
    position, _ = await repo.relay_state(session)
    return ORJSONResponse(
        content={
            "scans": await repo.scan_count(session),
            "maxScanId": await repo.max_scan_id(session),
            # Spec 008 FR-021: every committed scan has been published, and no event of
            # Checkout's is waiting in the outbox.
            "relayPosition": position,
            "unpublishedOutbox": await repo.unpublished_count(session),
        }
    )
