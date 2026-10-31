"""The broker's topics match events/topics.yaml (partitions, compaction, retention)."""

from __future__ import annotations

import pathlib

import yaml
from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType

from tests.broker.conftest import BOOTSTRAP, MANIFEST


async def test_every_topic_exists_with_the_declared_partitions_and_config():
    topics = yaml.safe_load(MANIFEST.read_text())["topics"]
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    try:
        described = {d["topic"]: d for d in await admin.describe_topics(list(topics))}
        for name, spec in topics.items():
            assert len(described[name]["partitions"]) == spec["partitions"], name
        configs = await admin.describe_configs(
            [ConfigResource(ConfigResourceType.TOPIC, name) for name in topics]
        )
        by_topic = {}
        for response in configs:
            for resource in response.resources:
                name = resource[3]
                by_topic[name] = {c[0]: c[1] for c in resource[4]}
        for name, spec in topics.items():
            if spec.get("compact"):
                assert by_topic[name]["cleanup.policy"] == "compact", name
            else:
                assert by_topic[name]["retention.ms"] == str(spec["retention_ms"]), name
    finally:
        await admin.close()


def test_the_init_script_is_generated_from_the_manifest():
    script = (pathlib.Path(MANIFEST).parent / "create-topics.sh").read_text()
    for name in yaml.safe_load(MANIFEST.read_text())["topics"]:
        assert f"--topic {name} " in script
