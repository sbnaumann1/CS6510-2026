"""Data access for the Inventory Service (inventory_service only).

Each function runs one statement against the caller's session and returns what
the database returned. None of them commit, roll back, or begin — the caller
owns the transaction boundary (002 research R4). Read-only: Inventory reports on
stock and alert rows that Checkout writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

SERVICE = "inventory"

# Live stock joined to the most recent alert row per SKU (001 data-model.md).
# The alert row supplies only the timestamp; currentStock always comes from live
# inventory. Ordering by alert time then SKU satisfies FR-007's "timestamp
# order" while staying deterministic for SKUs that have no alert row (possible
# when ?threshold= is raised above the configured default).
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


async def low_stock_report(session: AsyncSession, threshold: int) -> Sequence[Row]:
    return (await session.execute(_LOW_STOCK, {"threshold": threshold})).all()


async def now(session: AsyncSession) -> datetime:
    return await session.scalar(_NOW)
