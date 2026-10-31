"""Topics Analytics produces and consumes (spec 008 FR-018, contracts/events.md).

Checked against events/topics.yaml by scripts/check_boundaries.py (rules E3-E6): a topic
used here must be declared there, with this service among its producers or consumers.
Every consumer may also publish to the dead-letter topic.
"""

from __future__ import annotations

from checkout_web.events import CATALOG_ITEMS, CHECKOUT_SCANS, DEAD_LETTERS

PRODUCES: tuple[str, ...] = (DEAD_LETTERS,)
CONSUMES: tuple[str, ...] = (CHECKOUT_SCANS, CATALOG_ITEMS)
