"""Popular-items read path: the latest published window, from memory.

The scan id (Checkout's `transaction_item.id`) IS the scan sequence: there is no separate
counter (001 research R6). The persistent pipeline (pipeline.py) publishes each window as
it slides, so the read path is a dictionary lookup. Before the first window is published
the answer is computed on demand from the in-memory `ScanWindow`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from analytics_service import state
from analytics_service.catalog_client import get_snapshot
from analytics_service.config import settings

# Top 200 covers any sane ?limit=; names and rank are attached at read time, so the
# published ranking stays small.
RANKING_DEPTH = 200


async def _render(
    ranking: list[dict[str, Any]],
    limit: int,
    window_start: int,
    window_end: int,
    computed_at: str,
) -> dict[str, Any]:
    top = ranking[:limit]
    names = await get_snapshot().names([entry["sku"] for entry in top]) if top else {}
    return {
        "windowSize": settings.popular_window_size,
        "slideInterval": settings.popular_slide_interval,
        "windowStart": window_start,
        "windowEnd": window_end,
        "computedAt": computed_at,
        "items": [
            {
                "sku": entry["sku"],
                "name": names[entry["sku"]],
                "scanCount": entry["scanCount"],
                "rank": i + 1,
            }
            for i, entry in enumerate(top)
        ],
    }


async def read(limit: int) -> dict[str, Any]:
    latest = state.latest
    if latest is not None:
        return await _render(
            latest.ranking,
            limit,
            latest.window_start,
            latest.window_end,
            latest.computed_at.isoformat(),
        )

    # Nothing published yet: fewer than one slide into the run, or the process has just
    # started and is still waiting for Checkout. Answer over whatever scans are in the
    # in-memory window; this is the spec's "fewer than N items" edge case and the reason
    # windowStart can be 0.
    start, end = state.window.bounds()
    ranking = [
        {"sku": sku, "scanCount": count} for sku, count in state.window.counts(RANKING_DEPTH)
    ]
    return await _render(ranking, limit, start, end, datetime.now(timezone.utc).isoformat())
