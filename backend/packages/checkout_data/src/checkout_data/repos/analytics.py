"""Data access for the Analytics Service (analytics_service only).

Each function runs one statement against the caller's session and returns what
the database returned. None of them commit, roll back, or begin — the caller
owns the transaction boundary (002 research R4). Analytics reads the scan
sequence Checkout writes (`transaction_item`) and owns the window snapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

SERVICE = "analytics"

_MAX_SEQ = text("SELECT coalesce(max(id), 0) FROM transaction_item")

_GET_SCANS = text(
    "SELECT id, sku FROM transaction_item WHERE id > :last_id ORDER BY id LIMIT :limit"
)

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
VALUES (1, :window_size, :slide_interval, :window_start, :window_end, :computed_at,
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

_NOW = text("SELECT now()")


async def max_scan_seq(session: AsyncSession) -> int:
    return (await session.scalar(_MAX_SEQ)) or 0


async def get_scans_after(
    session: AsyncSession, last_id: int, limit: int = 1000
) -> Sequence[Row]:
    """Get (id, sku) for scans with id > last_id, ordered by id."""
    return (
        await session.execute(
            _GET_SCANS,
            {"last_id": last_id, "limit": limit},
        )
    ).all()


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
    computed_at: datetime | None = None,
) -> None:
    if computed_at is None:
        computed_at = await now(session)

    await session.execute(
        _UPSERT,
        {
            "window_size": window_size,
            "slide_interval": slide_interval,
            "window_start": start,
            "window_end": end,
            "computed_at": computed_at,
            "ranking": ranking_json,
        },
    )


async def read_snapshot(session: AsyncSession) -> Row | None:
    return (await session.execute(_READ)).first()


async def now(session: AsyncSession) -> datetime:
    return await session.scalar(_NOW)
