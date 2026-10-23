"""A cached snapshot of Catalog's public `GET /items` (spec 007 research R6).

The catalog is fixed during a run, so the list is fetched lazily on first need and kept
in memory: scans, the most frequent request, never make a per-scan call. An unknown SKU
triggers at most one refresh per `REFRESH_S`; after that it is simply not in the catalog.
Checkout stores the price on each scanned row, so completion never needs Catalog.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from checkout_service.config import settings
from checkout_web.client import ServiceClient
from checkout_web.errors import UpstreamUnavailable

REFRESH_S = 5.0
FETCH_TIMEOUT_S = 5.0

_transport: httpx.AsyncBaseTransport | None = None


class CatalogSnapshot:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = ServiceClient(base_url, transport=transport, peer="catalog")
        # sku -> (name, price_cents)
        self._by_sku: dict[str, tuple[str, int]] = {}
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
            self._by_sku = {
                i["sku"]: (i["name"], round(i["price"] * 100)) for i in response.json()["items"]
            }
            self._fetched_at = time.monotonic()

    async def get(self, sku: str) -> tuple[str, int] | None:
        """(name, price_cents), or None if Catalog does not list the SKU.

        Raises UpstreamUnavailable only when the cache is empty and Catalog is down."""
        entry = self._by_sku.get(sku)
        if entry is not None:
            return entry
        try:
            await self._refresh()
        except UpstreamUnavailable:
            if not self._by_sku:
                raise
        return self._by_sku.get(sku)

    async def warm(self) -> None:
        """Load the snapshot if it is empty. Raises UpstreamUnavailable if Catalog is down."""
        if not self._by_sku:
            await self._refresh()

    def name_of(self, sku: str) -> str:
        entry = self._by_sku.get(sku)
        return entry[0] if entry else sku

    async def aclose(self) -> None:
        await self._client.aclose()


_snapshot: CatalogSnapshot | None = None


def get_snapshot() -> CatalogSnapshot:
    global _snapshot
    if _snapshot is None:
        _snapshot = CatalogSnapshot(settings.catalog_url, _transport)
    return _snapshot


async def warm_forever(retry_s: float = 2.0) -> None:
    """Load the snapshot at startup, retrying until Catalog answers.

    Each worker process keeps its own snapshot, so each warms its own. Until it has, a call
    that needs Catalog fails cleanly if Catalog is down; afterwards Catalog's outages do not
    matter (spec 007 research R6)."""
    while True:
        try:
            await get_snapshot().warm()
            return
        except UpstreamUnavailable:
            await asyncio.sleep(retry_s)


def reset(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Forget the cache; tests pass an in-process transport."""
    global _snapshot, _transport
    _snapshot = None
    _transport = transport
