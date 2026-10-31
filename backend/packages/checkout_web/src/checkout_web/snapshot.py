"""A read-only, in-memory copy of Catalog's items, built from `catalog.items.v1`
(spec 008 research R3, data-model.md "Snapshots").

Catalog's items never change after seeding, so Catalog publishes the whole list once at
each startup, followed by a marker carrying the item count. A snapshot reads the topic
from the beginning, overwrites by SKU, and is *ready* once it has seen a marker and holds
at least that many SKUs. That is how an unknown SKU (404) is told apart from a snapshot that
has not loaded yet (503).

Data-free: the shape of an entry is whatever the caller's `parse` returns; this module
only knows SKUs, the marker and the topic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Generic, TypeVar

from checkout_web.bus import Bus, Consumer, Record
from checkout_web.consumer_loop import run_consumer
from checkout_web.events import CATALOG_ITEMS, CATALOG_PUBLISHED, ITEM_PUBLISHED

log = logging.getLogger("checkout.snapshot")

V = TypeVar("V")


class TopicSnapshot(Generic[V]):
    def __init__(self, bus: Bus, *, name: str, parse: Callable[[str, dict], V]) -> None:
        self._bus = bus
        self._name = name
        self._parse = parse
        self._items: dict[str, V] = {}
        self._expected: int | None = None
        self._ready = asyncio.Event()
        self._consumer: Consumer | None = None

    # ---- reading ---------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def get(self, sku: str) -> V | None:
        return self._items.get(sku)

    def __contains__(self, sku: str) -> bool:
        return sku in self._items

    def __len__(self) -> int:
        return len(self._items)

    async def wait_ready(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # ---- building --------------------------------------------------------------------

    async def run(self) -> None:
        """Read the topic forever. One record at a time, so the marker never overtakes the
        items published before it."""
        self._consumer = self._bus.consumer(CATALOG_ITEMS, start="beginning")
        await self._consumer.start()
        await run_consumer(
            self._consumer, self._apply, name=f"{self._name}-catalog-snapshot",
            bus=self._bus, concurrency=1,
        )

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()

    async def _apply(self, rec: Record) -> None:
        event = rec.event()
        if event.type == ITEM_PUBLISHED:
            self._items[event.data["sku"]] = self._parse(event.data["sku"], event.data)
        elif event.type == CATALOG_PUBLISHED:
            self._expected = int(event.data["itemCount"])
        else:
            return  # unknown types are ignored (compatibility rule)
        if self._expected is not None and len(self._items) >= self._expected:
            if not self._ready.is_set():
                log.info("%s: catalog snapshot ready (%d items)", self._name, len(self._items))
            self._ready.set()
