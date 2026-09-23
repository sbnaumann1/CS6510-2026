"""Data access for the Analytics layer.

Each function runs one statement against the caller's session and returns what
the database returned. None of them commit, roll back, or begin — the caller
owns the transaction boundary (research R4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

_MAX_SEQ = text("SELECT coalesce(max(id), 0) FROM transaction_item")

_WINDOW_COUNTS = text(
    "SELECT sku, count(*) AS scan_count"
    "  FROM transaction_item"
    " WHERE id > :window_start AND id <= :window_end"
    " GROUP BY sku"
    " ORDER BY count(*) DESC, sku"
    " LIMIT :depth"
)

_UPSERT = text(
    """
INSERT INTO popular_window_snapshot
       (id, window_size, slide_interval, window_start, window_end, computed_at, ranking)
VALUES (1, :window_size, :slide_interval, :window_start, :window_end, now(),
        CAST(:ranking AS jsonb))
ON CONFLICT (id) DO UPDATE
   SET window_size   = EXCLUDED.window_size,
       slide_interval = EXCLUDED.slide_interval,
       window_start  = EXCLUDED.window_start,
       window_end    = EXCLUDED.window_end,
       computed_at   = EXCLUDED.computed_at,
       ranking       = EXCLUDED.ranking
"""
)

_READ = text(
    "SELECT window_size, slide_interval, window_start, window_end, computed_at, ranking"
    "  FROM popular_window_snapshot WHERE id = 1"
)

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


async def max_scan_seq(session: AsyncSession) -> int:
    return (await session.scalar(_MAX_SEQ)) or 0


async def window_counts(
    session: AsyncSession, start: int, end: int, depth: int
) -> Sequence[Row]:
    """(sku, scan_count) for scans with start < id <= end, top `depth`."""
    return (
        await session.execute(
            _WINDOW_COUNTS,
            {"window_start": start, "window_end": end, "depth": depth},
        )
    ).all()


async def upsert_snapshot(
    session: AsyncSession,
    window_size: int,
    slide_interval: int,
    start: int,
    end: int,
    ranking_json: str,
) -> None:
    await session.execute(
        _UPSERT,
        {
            "window_size": window_size,
            "slide_interval": slide_interval,
            "window_start": start,
            "window_end": end,
            "ranking": ranking_json,
        },
    )


async def read_snapshot(session: AsyncSession) -> Row | None:
    return (await session.execute(_READ)).first()


async def low_stock_report(session: AsyncSession, threshold: int) -> Sequence[Row]:
    return (await session.execute(_LOW_STOCK, {"threshold": threshold})).all()


async def now(session: AsyncSession) -> datetime:
    return await session.scalar(_NOW)
