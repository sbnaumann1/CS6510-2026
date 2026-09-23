"""Out-of-band work: popular-window recompute."""

from __future__ import annotations

import logging

from app.config import settings
from app.db import POPULAR_RECOMPUTE_LOCK, SessionLocal, advisory_lock, engine
from app.services import analytics

log = logging.getLogger("checkout")


def should_recompute(scan_seq: int | None) -> bool:
    """True when this scan's sequence number crosses a slide boundary."""
    if not scan_seq:
        return False
    return scan_seq % settings.popular_slide_interval == 0


async def recompute_window() -> None:
    """Recompute the popular-items window, at most one worker at a time.

    Runs off the request's critical path. The advisory lock means a worker that
    loses the race skips instead of blocking — the next slide will recompute
    anyway, so a missed recompute costs nothing.
    """
    try:
        async with engine.connect() as conn:
            async with advisory_lock(conn, POPULAR_RECOMPUTE_LOCK) as acquired:
                if not acquired:
                    return
                async with SessionLocal() as session:
                    await analytics.recompute(session)
    except Exception:  # never let background work surface as a request error
        log.exception("popular-window recompute failed")
