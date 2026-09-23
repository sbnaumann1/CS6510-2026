"""Low-stock alert emission — the write side of low-stock tracking."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import transactions_repo


async def emit_crossings(
    session: AsyncSession,
    decremented: list[Any],
    qty_by_sku: dict[str, int],
    threshold: int,
) -> None:
    """Insert one alert row per SKU that crossed the threshold on this completion.

    Called inside the completion transaction, so the alert commits atomically
    with the decrement that caused it (SC-005). The crossing condition
    `current_stock < threshold <= current_stock + qty` fires once per descent
    rather than once per completion (FR-006).
    """
    crossings = [
        {"sku": row.sku, "current_stock": row.current_stock, "threshold": threshold}
        for row in decremented
        if row.current_stock < threshold <= row.current_stock + qty_by_sku[row.sku]
    ]
    if crossings:
        await transactions_repo.insert_alerts(session, crossings)
