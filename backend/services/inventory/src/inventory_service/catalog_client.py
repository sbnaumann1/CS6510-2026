"""A cached snapshot of Catalog's public `GET /items` (spec 007 research R6).

The catalog is fixed during a run, so the list is fetched lazily on first need and kept
in memory. An unknown SKU triggers at most one refresh per `REFRESH_S`. Inventory uses
it only for item names in the low-stock report.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from checkout_web.client import ServiceClient
from checkout_web.errors import UpstreamUnavailable
from inventory_service.config import settings

REFRESH_S = 5.0
FETCH_TIMEOUT_S = 5.0

_transport: httpx.AsyncBaseTransport | None = None


class CatalogSnapshot:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = ServiceClient(base_url, transport=transport, peer="catalog")
        self._by_sku: dict[str, str] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        async with self._lock:
            if self._by_sku and time.monotonic() - self._fetched_at < REFRESH_S:
                return
            response = await self._client.request(
                "GET", "/items", timeout=FETCH_TIMEOUT_S, retries=1, idempotent=True
            )
            if response.status_code != 200:
                raise UpstreamUnavailable("catalog", f"HTTP {response.status_code}")
            self._by_sku = {i["sku"]: i["name"] for i in response.json()["items"]}
            self._fetched_at = time.monotonic()

    async def names(self, skus: list[str]) -> dict[str, str]:
        """SKU -> name. Falls back to the SKU itself for an unknown item."""
        if not self._by_sku or any(s not in self._by_sku for s in skus):
            try:
                await self._refresh()
            except UpstreamUnavailable:
                if not self._by_sku:
                    raise
        return {s: self._by_sku.get(s, s) for s in skus}

    async def aclose(self) -> None:
        await self._client.aclose()


_snapshot: CatalogSnapshot | None = None


def get_snapshot() -> CatalogSnapshot:
    global _snapshot
    if _snapshot is None:
        _snapshot = CatalogSnapshot(settings.catalog_url, _transport)
    return _snapshot


def reset(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Forget the cache; tests pass an in-process transport."""
    global _snapshot, _transport
    _snapshot = None
    _transport = transport
