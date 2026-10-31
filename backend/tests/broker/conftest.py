"""Real-Kafka tests (spec 008 FR-020, research R13): the Kafka-specific properties a fake
cannot prove (topic configuration, keys landing on partitions, compaction settings, consumer
groups, replay from the retained tail).

They run only with BROKER_TESTS=1, against the broker published on 127.0.0.1:9094:

    docker compose up -d kafka kafka-init --wait           # just the broker and its topics
    BROKER_TESTS=1 uv run pytest tests/broker -q

The `bus` fixture in tests/conftest.py is then a KafkaBus, so the `stack` and `client`
fixtures run the four services in-process on the real broker.

Each test starts from freshly created topics: otherwise the previous test's events (and the
previous run's) would be replayed into this one.
"""

from __future__ import annotations

import asyncio
import os
import pathlib

import pytest
import pytest_asyncio
import yaml

BOOTSTRAP = os.environ.get("KAFKA_TEST_BOOTSTRAP", "127.0.0.1:9094")
MANIFEST = pathlib.Path(__file__).resolve().parents[2] / "events" / "topics.yaml"


@pytest.fixture(autouse=True)
def _requires_a_broker():
    if os.environ.get("BROKER_TESTS") != "1":
        pytest.skip("real-Kafka tests: set BROKER_TESTS=1 with a broker on 127.0.0.1:9094")


def topic_configs(spec: dict) -> dict[str, str]:
    if spec.get("compact"):
        return {"cleanup.policy": "compact"}
    return {"retention.ms": str(spec["retention_ms"])}


async def recreate_topics() -> None:
    """Delete and re-create every topic in events/topics.yaml, so each test starts empty."""
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic

    topics = yaml.safe_load(MANIFEST.read_text())["topics"]
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        doomed = [t for t in topics if t in existing]
        if doomed:
            await admin.delete_topics(doomed)
            for _ in range(100):  # deletion is asynchronous
                if not set(await admin.list_topics()) & set(doomed):
                    break
                await asyncio.sleep(0.1)
        new = [
            NewTopic(name=t, num_partitions=spec["partitions"], replication_factor=1,
                     topic_configs=topic_configs(spec))
            for t, spec in topics.items()
        ]
        for attempt in range(50):
            try:
                await admin.create_topics(new)
                break
            except Exception:  # a topic still being deleted: wait and retry
                if attempt == 49:
                    raise
                await asyncio.sleep(0.2)
    finally:
        await admin.close()


@pytest_asyncio.fixture(autouse=True)
async def fresh_topics(_requires_a_broker):
    await recreate_topics()
    yield
