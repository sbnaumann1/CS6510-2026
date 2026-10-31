"""Inventory's transactional outbox: no stock change without its outcome event, no event
without its stock change (spec 008 FR-012, research R7).

The handler inserts the outcome event into the outbox in the same local transaction as the
stock change (`repo.insert_outbox`). After COMMIT it calls `publish_now` (the fast path),
and the sweeper delivers anything that was not published (a crash between COMMIT and
publish, a broker outage). Delivery is at-least-once; Checkout's waiters resolve once and
drop the rest.
"""

from __future__ import annotations

import asyncio
import logging

from checkout_web.bus import Transient
from checkout_web.events import Event, from_dict
from checkout_web.request import log_step
from inventory_service import eventbus, repo
from inventory_service.db import AutocommitSessionLocal, SessionLocal

log = logging.getLogger("inventory")

SWEEP_INTERVAL_S = 1.0
UNPUBLISHED_AFTER_S = 2.0
PURGE_EVERY = 60  # sweeps
RETAIN_HOURS = 1


async def publish_now(topic: str, key: str, event: Event) -> bool:
    """Fast path, after COMMIT: publish and mark the row. Never raises; False means the
    sweeper will deliver it."""
    try:
        await eventbus.get_bus().publish(topic, event, key=key)
    except Transient as exc:
        log_step("publish", outcome="deferred", tx=key, topic=topic, type=event.type, error=str(exc))
        return False
    try:
        async with AutocommitSessionLocal() as session:
            await repo.mark_published(session, event.event_id)
    except Exception:  # published but unmarked: the sweeper republishes, Checkout drops the copy
        log.exception("could not mark outbox row published")
    log_step("publish", outcome="ok", tx=key, topic=topic, type=event.type)
    return True


async def sweep_once(age_s: float = UNPUBLISHED_AFTER_S, limit: int = 100) -> int:
    """Publish outbox rows still unpublished after `age_s`. Returns how many were sent."""
    sent = 0
    async with SessionLocal() as session:
        async with session.begin():
            rows = await repo.claim_unpublished(session, age_s, limit)
            for row in rows:
                try:
                    await eventbus.get_bus().publish(row.topic, from_dict(row.payload), key=row.key)
                except Transient:
                    break  # the broker is still down; the rest wait for the next sweep
                await repo.mark_published(session, row.event_id)
                log_step("publish", outcome="swept", tx=row.key, topic=row.topic, type=row.payload.get("type"))
                sent += 1
    return sent


async def sweeper_loop() -> None:
    """Safe in every worker: rows are claimed with SKIP LOCKED, and publishing twice is
    harmless."""
    n = 0
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        try:
            await sweep_once()
            n += 1
            if n % PURGE_EVERY == 0:
                async with SessionLocal() as session:
                    async with session.begin():
                        await repo.delete_published_older_than(session, RETAIN_HOURS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("outbox sweeper failed")
