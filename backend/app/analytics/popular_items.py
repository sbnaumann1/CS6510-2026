"""Popular-items analytics: hopping window over the global scan sequence.

`transaction_item.id` IS the scan sequence — there is no separate counter
(research R6). The window is recomputed out of band every `slideInterval` scans
into a single snapshot row, so every worker serves an identical ranking and the
read path is one primary-key fetch.
"""

from __future__ import annotations

from typing import Any

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog_cache
from app.config import settings
from app.db import analytics_repo as repo

# Top 200 covers any sane ?limit=; names and rank are attached at read time from
# the catalog cache, so the stored blob stays small.
RANKING_DEPTH = 200


def _bounds(max_seq: int) -> tuple[int, int]:
    """(window_start exclusive, window_end inclusive) for the current sequence."""
    window_start = max(0, max_seq - settings.popular_window_size)
    return window_start, max_seq


async def _counts(session: AsyncSession, start: int, end: int) -> list[dict[str, Any]]:
    rows = await repo.window_counts(session, start, end, RANKING_DEPTH)
    return [{"sku": r.sku, "scanCount": r.scan_count} for r in rows]


async def recompute(session: AsyncSession) -> None:
    """Recompute the window and replace the snapshot row."""
    start, end = _bounds(await repo.max_scan_seq(session))
    ranking = await _counts(session, start, end)

    await repo.upsert_snapshot(
        session,
        settings.popular_window_size,
        settings.popular_slide_interval,
        start,
        end,
        orjson.dumps(ranking).decode(),
    )
    await session.commit()


def _render(
    ranking: list[dict[str, Any]],
    limit: int,
    window_start: int,
    window_end: int,
    computed_at: str,
) -> dict[str, Any]:
    return {
        "windowSize": settings.popular_window_size,
        "slideInterval": settings.popular_slide_interval,
        "windowStart": window_start,
        "windowEnd": window_end,
        "computedAt": computed_at,
        "items": [
            {
                "sku": entry["sku"],
                "name": catalog_cache.name_of(entry["sku"]),
                "scanCount": entry["scanCount"],
                "rank": i + 1,
            }
            for i, entry in enumerate(ranking[:limit])
        ],
    }


async def read(session: AsyncSession, limit: int) -> dict[str, Any]:
    row = await repo.read_snapshot(session)
    if row is not None:
        return _render(
            row.ranking,
            limit,
            row.window_start,
            row.window_end,
            row.computed_at.isoformat(),
        )

    # No snapshot yet — fewer than one slide into the run. Compute on demand over
    # whatever scans exist; this is the spec's "fewer than N items" edge case and
    # the reason windowStart can be 0.
    start, end = _bounds(await repo.max_scan_seq(session))
    ranking = await _counts(session, start, end)
    now = await repo.now(session)
    return _render(ranking, limit, start, end, now.isoformat())
