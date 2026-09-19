"""Inventory service: the low-stock view and alert-crossing emission."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Live stock joined to the most recent alert row per SKU (data-model.md). The
# alert row supplies only the timestamp; currentStock always comes from live
# inventory. Ordering by alert time then SKU satisfies FR-007's "timestamp order"
# while staying deterministic for SKUs that have no alert row (possible when
# ?threshold= is raised above the configured default).
_LOW_STOCK = text(
    """
SELECT c.sku, c.name, s.current_stock,
       COALESCE(a.triggered_at, now()) AS triggered_at
  FROM inventory_stock s
  JOIN catalog_item c USING (sku)
  LEFT JOIN LATERAL (
        SELECT triggered_at FROM low_stock_alert
         WHERE sku = s.sku ORDER BY triggered_at DESC LIMIT 1
  ) a ON TRUE
 WHERE s.current_stock < :threshold
 ORDER BY a.triggered_at NULLS LAST, c.sku
"""
)

_NOW = text("SELECT now()")

_INSERT_ALERT = text(
    "INSERT INTO low_stock_alert (sku, current_stock, threshold)"
    " VALUES (:sku, :current_stock, :threshold)"
)


async def low_stock(session: AsyncSession, threshold: int) -> dict[str, Any]:
    rows = (await session.execute(_LOW_STOCK, {"threshold": threshold})).all()
    generated_at = await session.scalar(_NOW)
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
        await session.execute(_INSERT_ALERT, crossings)
