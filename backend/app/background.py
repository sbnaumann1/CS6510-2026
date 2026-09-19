"""Out-of-band work: window recompute and the abandoned-transaction sweeper."""

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


async def sweep_abandoned() -> int:
    """Cancel OPEN transactions older than TX_ABANDON_MINUTES.

    No stock is decremented for them — stock moves only at completion (INV-6).
    Uses the transaction_open_started_idx partial index.
    """
    from sqlalchemy import text

    async with SessionLocal() as session:
        result = await session.execute(
            text(
                "UPDATE transaction SET status = 'CANCELLED'"
                " WHERE status = 'OPEN'"
                "   AND started_at < now() - make_interval(mins => :mins)"
            ),
            {"mins": settings.tx_abandon_minutes},
        )
        await session.commit()
        return result.rowcount or 0
