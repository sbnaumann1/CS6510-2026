"""Abandoned-transaction sweeper."""

from __future__ import annotations

from checkout_service import repo
from checkout_service.config import settings
from checkout_service.db import SessionLocal


async def sweep_abandoned() -> int:
    """Cancel OPEN transactions older than TX_ABANDON_MINUTES.

    No stock is touched for them (INV-6): stock moves only when Inventory reserves it
    at completion. COMPLETING transactions are never swept.
    """
    async with SessionLocal() as session:
        n = await repo.sweep_abandoned(session, settings.tx_abandon_minutes)
        await session.commit()
        return n
