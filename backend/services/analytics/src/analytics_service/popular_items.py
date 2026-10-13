"""Popular-items read path: the latest published window.

`transaction_item.id` IS the scan sequence — there is no separate counter
(001 research R6). The persistent pipeline (pipeline.py) publishes each window
into a single snapshot row as it slides, so every worker serves an identical
ranking and the read path is one primary-key fetch.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from checkout_data import catalog as catalog_cache
from checkout_data.config import settings
from checkout_data.repos import analytics as repo

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
