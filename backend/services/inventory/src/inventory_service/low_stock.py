"""Low-stock report — the read side of low-stock tracking."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from inventory_service import repo
from inventory_service.catalog_snapshot import get_snapshot


async def read(session: AsyncSession, threshold: int) -> dict[str, Any]:
    rows = await repo.low_stock_report(session, threshold)
    generated_at = await repo.now(session)
    names = await get_snapshot().names([r.sku for r in rows]) if rows else {}
    return {
        "threshold": threshold,
        "generatedAt": generated_at.isoformat(),
        "alerts": [
            {
                "sku": r.sku,
                "name": names[r.sku],
                "currentStock": r.current_stock,
                "threshold": threshold,
                "triggeredAt": r.triggered_at.isoformat(),
            }
            for r in rows
        ],
    }
