"""Low-stock report — the read side of low-stock tracking."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from checkout_data.repos import inventory as repo


async def read(session: AsyncSession, threshold: int) -> dict[str, Any]:
    rows = await repo.low_stock_report(session, threshold)
    generated_at = await repo.now(session)
    return {
        "threshold": threshold,
        "generatedAt": generated_at.isoformat(),
        "alerts": [
            {
                "sku": r.sku,
                "name": r.name,
                "currentStock": r.current_stock,
                "threshold": threshold,
                "triggeredAt": r.triggered_at.isoformat(),
            }
            for r in rows
        ],
    }
