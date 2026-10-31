#!/usr/bin/env python
"""Render events/topics.yaml as the shell script the `kafka-init` job runs (spec 008).

    uv run python scripts/create_topics.py            # print the script
    uv run python scripts/create_topics.py --write    # rewrite events/create-topics.sh

events/topics.yaml is the single source of truth. The committed events/create-topics.sh is
generated from it (the apache/kafka image has no YAML parser, so the init job runs a plain
script), and tests/unit/test_events.py fails if the two drift.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "events" / "topics.yaml"
SCRIPT = ROOT / "events" / "create-topics.sh"


def render() -> str:
    topics = yaml.safe_load(MANIFEST.read_text())["topics"]
    lines = [
        "#!/usr/bin/env bash",
        "# GENERATED from events/topics.yaml by scripts/create_topics.py. Do not edit.",
        "# Creates every topic idempotently, then exits 0 (the compose `kafka-init` job).",
        "set -euo pipefail",
        'BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"',
        'KT=/opt/kafka/bin/kafka-topics.sh',
        "",
    ]
    for name, spec in topics.items():
        cfg = ["cleanup.policy=compact"] if spec.get("compact") else [f"retention.ms={spec['retention_ms']}"]
        config = " ".join(f"--config {c}" for c in cfg)
        lines.append(
            f'"$KT" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists '
            f"--topic {name} --partitions {spec['partitions']} --replication-factor 1 {config}"
        )
    lines += ["", '"$KT" --bootstrap-server "$BOOTSTRAP" --list', ""]
    return "\n".join(lines)


if __name__ == "__main__":
    text = render()
    if "--write" in sys.argv:
        SCRIPT.write_text(text)
        print(f"wrote {SCRIPT}")
    else:
        sys.stdout.write(text)
