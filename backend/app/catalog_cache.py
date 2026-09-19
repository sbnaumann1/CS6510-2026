"""In-process catalog cache.

The catalog is immutable during a run, so it is loaded once at startup into a dict
and a pre-rendered `GET /items` body (research R7). Ascending SKU order is load-
bearing: the load client derives its Zipf popularity rank from catalog response
position (research R12), so the order decides which SKUs are hot.
"""

from __future__ import annotations

import orjson
from sqlalchemy import select

from app.models import CatalogItem
from app.money import cents_to_amount

# sku -> (name, price_cents)
_by_sku: dict[str, tuple[str, int]] = {}
_items_body: bytes = b'{"items":[]}'


async def load(session) -> int:
    """Populate the cache from the database. Returns the item count."""
    global _by_sku, _items_body

    rows = (
        await session.execute(
            select(CatalogItem.sku, CatalogItem.name, CatalogItem.price_cents).order_by(
                CatalogItem.sku
            )
        )
    ).all()

    _by_sku = {sku: (name, cents) for sku, name, cents in rows}
    _items_body = orjson.dumps(
        {
            "items": [
                {"sku": sku, "name": name, "price": cents_to_amount(cents)}
                for sku, name, cents in rows
            ]
        }
    )
    return len(rows)


def get(sku: str) -> tuple[str, int] | None:
    """(name, price_cents) for a SKU, or None if it is not in the catalog."""
    return _by_sku.get(sku)


def name_of(sku: str) -> str:
    entry = _by_sku.get(sku)
    return entry[0] if entry else sku


def items_body() -> bytes:
    """Pre-rendered GET /items response body."""
    return _items_body


def size() -> int:
    return len(_by_sku)
