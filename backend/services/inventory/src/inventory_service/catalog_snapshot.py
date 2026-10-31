"""Inventory's snapshot of item names, built from `catalog.items.v1` (spec 008 research R3;
replaces 007's catalog_client).

Owner: Catalog. Source topic: `catalog.items.v1`. Holds SKU -> name, read-only. Rebuilt from
the topic alone after a restart: Inventory never calls Catalog. A name the snapshot does not
have yet is served as the SKU itself, so the low-stock report never fails because of Catalog.
"""

from __future__ import annotations

from checkout_web.bus import Bus
from checkout_web.snapshot import TopicSnapshot
from inventory_service import eventbus


class CatalogSnapshot:
    def __init__(self, bus: Bus) -> None:
        self._snap: TopicSnapshot[str] = TopicSnapshot(
            bus, name="inventory", parse=lambda sku, d: d["name"]
        )

    @property
    def ready(self) -> bool:
        return self._snap.ready

    async def names(self, skus: list[str]) -> dict[str, str]:
        """SKU -> name. Falls back to the SKU itself for an unknown item."""
        return {s: self._snap.get(s) or s for s in skus}

    async def run(self) -> None:
        await self._snap.run()

    async def wait_ready(self, timeout: float | None = None) -> bool:
        return await self._snap.wait_ready(timeout)


_snapshot: CatalogSnapshot | None = None


def get_snapshot() -> CatalogSnapshot:
    global _snapshot
    if _snapshot is None:
        _snapshot = CatalogSnapshot(eventbus.get_bus())
    return _snapshot


def reset() -> None:
    """Forget the snapshot; the next start builds a fresh one from the topic."""
    global _snapshot
    _snapshot = None
