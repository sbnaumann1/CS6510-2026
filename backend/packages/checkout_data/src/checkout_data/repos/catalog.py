"""Read-only catalog access, importable by every service.

Each function runs one statement against the caller's session and returns what
the database returned. None of them commit, roll back, or begin.
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from checkout_data.models import CatalogItem

SERVICE = "catalog"


async def load_catalog(session: AsyncSession) -> Sequence[Row]:
    """(sku, name, price_cents) for every item, in ascending SKU order."""
    return (
        await session.execute(
            select(CatalogItem.sku, CatalogItem.name, CatalogItem.price_cents).order_by(
                CatalogItem.sku
            )
        )
    ).all()
