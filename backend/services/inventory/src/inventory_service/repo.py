"""Data access for the Inventory Service's reporting reads.

Each function runs one statement against the caller's session. None of them commit
or begin. The reservation statements live in reserve.py. Item names are not joined
here: they come from the snapshot of Catalog's events (catalog_snapshot.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

import orjson
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from checkout_web.events import Event

# Live stock joined to the most recent alert row per SKU (001 data-model.md). The alert
# row supplies only the timestamp; currentStock always comes from live inventory.
# Ordering by alert time then SKU satisfies FR-007's "timestamp order" while staying
# deterministic for SKUs that have no alert row (possible when ?threshold= is raised
# above the configured default).
_LOW_STOCK = text(
    """
SELECT s.sku, s.current_stock,
       COALESCE(a.triggered_at, now()) AS triggered_at
  FROM inventory_stock s
  LEFT JOIN LATERAL (
        SELECT triggered_at FROM low_stock_alert
         WHERE sku = s.sku ORDER BY triggered_at DESC LIMIT 1
  ) a ON TRUE
 WHERE s.current_stock < :threshold
 ORDER BY a.triggered_at NULLS LAST, s.sku
"""
)

_STOCK_REPORT = text(
    "SELECT sku, initial_stock, current_stock FROM inventory_stock ORDER BY sku"
)

_ADJUSTMENTS = text(
    "SELECT transaction_id, state FROM stock_adjustment ORDER BY transaction_id"
)

_NOW = text("SELECT now()")


async def low_stock_report(session: AsyncSession, threshold: int) -> Sequence[Row]:
    return (await session.execute(_LOW_STOCK, {"threshold": threshold})).all()


async def stock_report(session: AsyncSession) -> Sequence[Row]:
    return (await session.execute(_STOCK_REPORT)).all()


async def adjustments(session: AsyncSession) -> Sequence[Row]:
    return (await session.execute(_ADJUSTMENTS)).all()


async def now(session: AsyncSession) -> datetime:
    return await session.scalar(_NOW)


# --- outbox (spec 008) ------------------------------------------------------------------

_INSERT_OUTBOX = text(
    "INSERT INTO outbox (topic, key, event_id, payload)"
    " VALUES (:topic, :key, CAST(:event_id AS uuid), CAST(:payload AS jsonb))"
)
_MARK_PUBLISHED = text(
    "UPDATE outbox SET published_at = now()"
    " WHERE event_id = CAST(:event_id AS uuid) AND published_at IS NULL"
)
_CLAIM_UNPUBLISHED = text(
    "SELECT id, topic, key, payload, event_id::text AS event_id FROM outbox"
    " WHERE published_at IS NULL AND created_at < now() - make_interval(secs => :age)"
    " ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
)
_DELETE_PUBLISHED = text(
    "DELETE FROM outbox WHERE published_at IS NOT NULL"
    " AND published_at < now() - make_interval(hours => :hours)"
)
_UNPUBLISHED_COUNT = text(
    "SELECT count(*) FROM outbox WHERE published_at IS NULL"
    " AND created_at < now() - make_interval(secs => :age)"
)


async def insert_outbox(session: AsyncSession, topic: str, key: str, event: Event) -> None:
    await session.execute(
        _INSERT_OUTBOX,
        {
            "topic": topic,
            "key": key,
            "event_id": event.event_id,
            "payload": orjson.dumps(event.to_dict()).decode(),
        },
    )


async def mark_published(session: AsyncSession, event_id: str) -> None:
    await session.execute(_MARK_PUBLISHED, {"event_id": event_id})


async def claim_unpublished(
    session: AsyncSession, age_s: float = 2.0, limit: int = 100
) -> Sequence[Row]:
    return (await session.execute(_CLAIM_UNPUBLISHED, {"age": age_s, "n": limit})).all()


async def delete_published_older_than(session: AsyncSession, hours: int = 1) -> int:
    return (await session.execute(_DELETE_PUBLISHED, {"hours": hours})).rowcount or 0


async def unpublished_count(session: AsyncSession, age_s: float = 2.0) -> int:
    return int(await session.scalar(_UNPUBLISHED_COUNT, {"age": age_s}) or 0)
