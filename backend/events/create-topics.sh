#!/usr/bin/env bash
# GENERATED from events/topics.yaml by scripts/create_topics.py. Do not edit.
# Creates every topic idempotently, then exits 0 (the compose `kafka-init` job).
set -euo pipefail
BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"
KT=/opt/kafka/bin/kafka-topics.sh

"$KT" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists --topic catalog.items.v1 --partitions 1 --replication-factor 1 --config cleanup.policy=compact
"$KT" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists --topic checkout.scans.v1 --partitions 1 --replication-factor 1 --config retention.ms=604800000
"$KT" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists --topic checkout.completions.v1 --partitions 6 --replication-factor 1 --config retention.ms=86400000
"$KT" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists --topic inventory.stock.v1 --partitions 6 --replication-factor 1 --config retention.ms=86400000
"$KT" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists --topic dead-letters.v1 --partitions 1 --replication-factor 1 --config retention.ms=1209600000

"$KT" --bootstrap-server "$BOOTSTRAP" --list
