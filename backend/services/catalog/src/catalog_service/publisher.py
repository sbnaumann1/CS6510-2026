"""Publishes the catalog to `catalog.items.v1` at startup (spec 008 research R3).

The catalog is fixed after seeding, so there is no stream of changes to announce: this
hands the whole list to whoever needs it, once per start. One `catalog.item-published`
per item (key = SKU), then one `catalog.catalog-published` marker with the item count. A
consumer is ready once it has seen the marker and that many SKUs.

Republishing on every start is what makes consumers independent of Catalog's availability:
the topic is compacted (latest record per SKU kept forever), so a consumer that starts or
restarts later rebuilds from the topic and never calls Catalog. Duplicates are harmless:
a record per SKU overwrites itself.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from catalog_service import store
from checkout_web.bus import Bus, Transient
from checkout_web.events import (
    CATALOG_ITEMS,
    CATALOG_PUBLISHED,
    ITEM_PUBLISHED,
    MARKER_KEY,
    make_event,
)
from checkout_web.request import log_step

log = logging.getLogger("checkout")

PRODUCER = "catalog"
_CHUNK = 250  # events per batch; the producer sends each batch in a few requests


async def publish_catalog(bus: Bus, *, retry_s: float = 1.0) -> int:
    """Publish every item, then the marker. Retries until the broker has accepted all of
    it, then returns the item count."""
    rows = store.rows()
    publish_id = str(uuid.uuid4())
    while True:
        try:
            for i in range(0, len(rows), _CHUNK):
                await bus.publish_batch(
                    CATALOG_ITEMS,
                    [
                        (
                            make_event(
                                ITEM_PUBLISHED,
                                PRODUCER,
                                {"sku": sku, "name": name, "priceCents": cents},
                                correlation_id=publish_id,
                            ),
                            sku,
                        )
                        for sku, name, cents in rows[i : i + _CHUNK]
                    ],
                )
            await bus.publish(
                CATALOG_ITEMS,
                make_event(
                    CATALOG_PUBLISHED,
                    PRODUCER,
                    {"publishId": publish_id, "itemCount": len(rows)},
                    correlation_id=publish_id,
                ),
                key=MARKER_KEY,
            )
        except Transient as exc:
            log.warning("catalog not published yet (%s); retrying", exc)
            await asyncio.sleep(retry_s)
            continue
        log_step("publish-catalog", outcome="ok", items=len(rows), publish_id=publish_id)
        return len(rows)
