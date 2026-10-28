"""Catalog's publication on a real compacted topic: republishing is harmless, and a service
that starts later rebuilds its snapshot from the topic alone (research R3)."""

from __future__ import annotations

import asyncio

from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType

from catalog_service import publisher, store
from catalog_service.main import load as load_catalog
from checkout_web import events as ev
from checkout_web.snapshot import TopicSnapshot
from tests.broker.conftest import BOOTSTRAP


async def test_the_catalog_topic_is_compacted(reset_db):
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    try:
        responses = await admin.describe_configs([ConfigResource(ConfigResourceType.TOPIC, ev.CATALOG_ITEMS)])
        configs = {c[0]: c[1] for r in responses for res in r.resources for c in res[4]}
        assert configs["cleanup.policy"] == "compact"
    finally:
        await admin.close()


async def test_publishing_twice_and_a_late_reader_gives_one_entry_per_sku(reset_db, bus):
    load_catalog()
    await publisher.publish_catalog(bus)
    await publisher.publish_catalog(bus)  # a restart: the same items again, a second marker

    snap = TopicSnapshot(bus, name="late", parse=lambda sku, d: (d["name"], d["priceCents"]))
    reader = asyncio.create_task(snap.run())
    try:
        assert await snap.wait_ready(15.0), "ready after the marker, from the real topic"
        assert len(snap) == store.size()
        sku, name, cents = store.rows()[0]
        assert snap.get(sku) == (name, cents)
    finally:
        reader.cancel()
