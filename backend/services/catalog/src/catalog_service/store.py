"""Catalog's whole data layer: one CSV file, loaded whole into memory.

The file has the header `sku,name,price_cents`, one row per item in ascending SKU
order (the load client derives its Zipf popularity rank from response position,
001 research R12). It is written once by the seed job and read-only afterwards.
"""

from __future__ import annotations

import csv
from pathlib import Path

HEADER = ["sku", "name", "price_cents"]

# (sku, name, price_cents) in ascending SKU order.
_items: list[tuple[str, str, int]] = []


class CatalogFileError(Exception):
    """The catalog file is missing or invalid; startup must stop."""


def load(path: str | Path) -> int:
    """Read and validate the file, replacing the in-memory list. Returns the item count."""
    global _items
    p = Path(path)
    try:
        fh = p.open(newline="", encoding="utf-8")
    except OSError as exc:
        raise CatalogFileError(f"cannot open catalog file {p}: {exc}") from exc
    items: list[tuple[str, str, int]] = []
    with fh:
        reader = csv.reader(fh)
        if next(reader, None) != HEADER:
            raise CatalogFileError(f"{p}:1: header must be exactly {','.join(HEADER)}")
        seen: set[str] = set()
        previous = ""
        for line, row in enumerate(reader, start=2):
            if len(row) != 3:
                raise CatalogFileError(f"{p}:{line}: expected 3 columns, got {len(row)}")
            sku, name, price = row
            if not sku or not name:
                raise CatalogFileError(f"{p}:{line}: sku and name must be non-empty")
            if sku in seen:
                raise CatalogFileError(f"{p}:{line}: duplicate sku {sku}")
            if sku < previous:
                raise CatalogFileError(f"{p}:{line}: rows must be in ascending sku order")
            try:
                cents = int(price)
            except ValueError:
                raise CatalogFileError(f"{p}:{line}: price_cents is not an integer") from None
            if cents <= 0:
                raise CatalogFileError(f"{p}:{line}: price_cents must be > 0")
            seen.add(sku)
            previous = sku
            items.append((sku, name, cents))
    _items = items
    return len(items)


def rows() -> list[tuple[str, str, int]]:
    return list(_items)


def size() -> int:
    return len(_items)


def clear() -> None:
    _items.clear()
