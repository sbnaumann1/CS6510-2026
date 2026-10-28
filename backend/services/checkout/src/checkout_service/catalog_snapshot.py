"""Checkout's snapshot of Catalog's items, built from `catalog.items.v1`
(spec 008 research R3; replaces 007's catalog_client).

Owner: Catalog. Source topic: `catalog.items.v1`. Holds SKU -> (name, price in cents),
read-only. Scans price from it and never call anyone. While the snapshot is not ready
(before Catalog's marker has been read) a lookup raises `UpstreamUnavailable`, so a cold
start is a clean 503 and an unknown SKU is still a clean 404.
"""

from __future__ import annotations

from checkout_service import eventbus
from checkout_web.bus import Bus
from checkout_web.errors import UpstreamUnavailable
from checkout_web.snapshot import TopicSnapshot


class CatalogSnapshot:
    def __init__(self, bus: Bus) -> None:
        self._snap: TopicSnapshot[tuple[str, int]] = TopicSnapshot(
            bus, name="checkout", parse=lambda sku, d: (d["name"], int(d["priceCents"]))
        )

    @property
    def ready(self) -> bool:
        return self._snap.ready

    async def get(self, sku: str) -> tuple[str, int] | None:
        """(name, price_cents), or None if Catalog does not list the SKU.

        Raises UpstreamUnavailable while the snapshot has not loaded yet."""
        if not self._snap.ready:
            raise UpstreamUnavailable("catalog", "the catalog has not loaded yet")
        return self._snap.get(sku)

    def name_of(self, sku: str) -> str:
        entry = self._snap.get(sku)
        return entry[0] if entry else sku

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
