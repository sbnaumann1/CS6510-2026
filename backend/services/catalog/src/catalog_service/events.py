"""Topics Catalog produces and consumes (spec 008 FR-018, contracts/events.md).

Checked against events/topics.yaml by scripts/check_boundaries.py (rules E3-E6): a topic
used here must be declared there, with this service among its producers or consumers.
"""

from __future__ import annotations

from checkout_web.events import CATALOG_ITEMS

PRODUCES: tuple[str, ...] = (CATALOG_ITEMS,)
CONSUMES: tuple[str, ...] = ()
