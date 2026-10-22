"""Data access for the Inventory Service's reporting reads.

Each function runs one statement against the caller's session. None of them commit
or begin. The reservation statements live in reserve.py. Item names are not joined
here: they come from Catalog's public API (catalog_client.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

# Live stock joined to the most recent alert row per SKU (001 data-model.md). The alert
# row supplies only the timestamp; currentStock always comes from live inventory.
# Ordering by alert time then SKU satisfies FR-007's "timestamp order" while staying
# deterministic for SKUs that have no alert row (possible when ?threshold= is raised
# above the configured default).
_LOW_STOCK = text(
    """
SELECT s.sku, s.current_stock,
       COALESCE(a.triggered_at, now()) AS triggered_at
  FROM inventory_stock s
  LEFT JOIN LATERAL (
        SELECT triggered_at FROM low_stock_alert
         WHERE sku = s.sku ORDER BY triggered_at DESC LIMIT 1
  ) a ON TRUE
 WHERE s.current_stock < :threshold
 ORDER BY a.triggered_at NULLS LAST, s.sku
"""
)

_STOCK_REPORT = text(
    "SELECT sku, initial_stock, current_stock FROM inventory_stock ORDER BY sku"
)

_ADJUSTMENTS = text(
    "SELECT transaction_id, state FROM stock_adjustment ORDER BY transaction_id"
)

_NOW = text("SELECT now()")


async def low_stock_report(session: AsyncSession, threshold: int) -> Sequence[Row]:
    return (await session.execute(_LOW_STOCK, {"threshold": threshold})).all()


async def stock_report(session: AsyncSession) -> Sequence[Row]:
    return (await session.execute(_STOCK_REPORT)).all()


async def adjustments(session: AsyncSession) -> Sequence[Row]:
    return (await session.execute(_ADJUSTMENTS)).all()


async def now(session: AsyncSession) -> datetime:
    return await session.scalar(_NOW)
