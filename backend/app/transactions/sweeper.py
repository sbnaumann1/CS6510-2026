"""Abandoned-transaction sweeper."""

from __future__ import annotations

from app.config import settings
from app.db import SessionLocal, transactions_repo


async def sweep_abandoned() -> int:
    """Cancel OPEN transactions older than TX_ABANDON_MINUTES.

    No stock is decremented for them — stock moves only at completion (INV-6).
    """
    async with SessionLocal() as session:
        n = await transactions_repo.sweep_abandoned(session, settings.tx_abandon_minutes)
        await session.commit()
        return n
