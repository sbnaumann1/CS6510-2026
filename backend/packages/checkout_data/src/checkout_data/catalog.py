"""In-process catalog cache, one per service process.

The catalog is immutable during a run, so each service that needs it loads it
once at startup (001 research R7). Catalog, Checkout and Analytics each hold
their own copy, read from the shared database — never from another service.
Ascending SKU order is load-bearing: the load client derives its Zipf
popularity rank from catalog response position (001 research R12).
"""

from __future__ import annotations

from checkout_data.repos.catalog import load_catalog

# sku -> (name, price_cents), in ascending SKU order (dicts keep insertion order)
_by_sku: dict[str, tuple[str, int]] = {}


async def load(session) -> int:
    """Populate the cache from the database. Returns the item count."""
    global _by_sku
    _by_sku = {sku: (name, cents) for sku, name, cents in await load_catalog(session)}
    return len(_by_sku)


def get(sku: str) -> tuple[str, int] | None:
    """(name, price_cents) for a SKU, or None if it is not in the catalog."""
    return _by_sku.get(sku)


def name_of(sku: str) -> str:
    entry = _by_sku.get(sku)
    return entry[0] if entry else sku


def size() -> int:
    return len(_by_sku)


def rows() -> list[tuple[str, str, int]]:
    """(sku, name, price_cents) in ascending SKU order."""
    return [(sku, name, cents) for sku, (name, cents) in _by_sku.items()]
