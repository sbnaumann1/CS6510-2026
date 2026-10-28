"""The scan relay: publishes Checkout's scans to `checkout.scans.v1` for Analytics
(spec 008 research R8).

The scan table (`transaction_item`) is the outbox: a scan is durable the moment its row
commits, and the relay turns committed rows into events. That costs the scan hot path
nothing (spec 006); an extra outbox write per scan would.

* **One writer.** A PostgreSQL advisory lock, taken in Checkout's own database, makes one
  worker at a time the relay. The others retry every second, and one takes over within a
  second if the leader's connection drops.
* **In order.** Events go out in scan-id order into the topic's single partition, so
  Analytics reads them in order. Only the contiguous prefix is published: a missing id means a
  transaction that has not committed yet, and it is skipped as a permanent gap only after
  `relay_gap_grace_ms` (the rule Analytics' tail ingestion used in 007).
* **At least once.** The position is saved after Kafka acknowledges a batch, so a crash
  republishes a few scans. Analytics ignores any scan id it has already applied.
* **A replaced log.** If `scan_log.token` differs from the one the position was saved
  under (a reset or restore), the relay publishes `checkout.scan-log-reset` and starts over.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import text

from checkout_service import eventbus, repo
from checkout_service.config import settings
from checkout_service.db import AutocommitSessionLocal, SessionLocal
from checkout_web.bus import Transient
from checkout_web.events import CHECKOUT_SCANS, SCAN_LOG_RESET, SCAN_RECORDED, make_event
from checkout_web.request import log_step

log = logging.getLogger("checkout")

PRODUCER = "checkout"
BATCH = 500
IDLE_S = 0.05
CONTEND_S = 1.0


class ScanRelay:
    def __init__(self, gap_grace_s: float | None = None) -> None:
        self._grace = settings.relay_gap_grace_ms / 1000 if gap_grace_s is None else gap_grace_s
        # (the id we are waiting for, when we first saw it missing)
        self._gap: tuple[int, float] | None = None
        self.passes = 0  # completed passes; tests wait for the first one

    async def run_pass(self) -> int:
        """Publish what is ready. Returns the number of scan events published (0 when idle
        or when waiting out a gap). Raises Transient if the broker is unavailable, leaving
        the saved position untouched."""
        async with SessionLocal() as session:
            pos, saved_token = await repo.relay_state(session)
            token = await repo.log_token(session)
            rows = await repo.relay_batch(session, pos, BATCH) if token == saved_token else []
        if not saved_token:
            # A fresh database: nothing was ever published, so there is nothing to reset.
            async with SessionLocal() as session:
                async with session.begin():
                    await repo.relay_save(session, 0, token)
            log_step("relay", outcome="initialised", log_id=token)
            self.passes += 1
            return 0
        if token != saved_token:
            await eventbus.get_bus().publish(
                CHECKOUT_SCANS,
                make_event(SCAN_LOG_RESET, PRODUCER, {"logId": token}, correlation_id=None),
                key=None,
            )
            async with SessionLocal() as session:
                async with session.begin():
                    await repo.relay_save(session, 0, token)
            self._gap = None
            self.passes += 1
            log_step("relay", outcome="log-reset", log_id=token)
            return 0

        self.passes += 1
        ready: list = []
        expected = pos + 1
        for row in rows:
            if row.id != expected:
                # Ids expected..row.id-1 have not committed (yet). Wait out the grace.
                now = time.monotonic()
                if self._gap is None or self._gap[0] != expected:
                    self._gap = (expected, now)
                if now - self._gap[1] < self._grace:
                    break
                log_step("relay", outcome="gap-skipped", from_id=expected, to_id=row.id - 1)
            ready.append(row)
            expected = row.id + 1
            self._gap = None
        if not ready:
            return 0

        await eventbus.get_bus().publish_batch(
            CHECKOUT_SCANS,
            [
                (
                    make_event(
                        SCAN_RECORDED,
                        PRODUCER,
                        {"scanId": r.id, "transactionId": f"tx-{r.transaction_id}", "sku": r.sku},
                        correlation_id=None,
                    ),
                    None,
                )
                for r in ready
            ],
        )
        async with SessionLocal() as session:
            async with session.begin():
                await repo.relay_save(session, ready[-1].id, token)
        return len(ready)

    async def run(self) -> None:
        """Contend for the lock; while holding it, publish forever."""
        while True:
            try:
                async with AutocommitSessionLocal() as lock_session:
                    if not await repo.relay_try_lock(lock_session):
                        await asyncio.sleep(CONTEND_S)
                        continue
                    log_step("relay", outcome="leader")
                    try:
                        await self._lead(lock_session)
                    finally:
                        try:
                            await repo.relay_unlock(lock_session)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scan relay failed; contending again")
                await asyncio.sleep(CONTEND_S)

    async def _lead(self, lock_session) -> None:
        checked = time.monotonic()
        while True:
            try:
                n = await self.run_pass()
            except Transient as exc:
                log_step("relay", outcome="broker-unavailable", error=str(exc))
                await asyncio.sleep(CONTEND_S)
                continue
            if time.monotonic() - checked > 5:
                await lock_session.execute(text("SELECT 1"))  # raises if the lock's connection died
                checked = time.monotonic()
            if n < BATCH:
                await asyncio.sleep(IDLE_S)


_relay: ScanRelay | None = None


def get_relay() -> ScanRelay:
    global _relay
    if _relay is None:
        _relay = ScanRelay()
    return _relay


def reset() -> None:
    global _relay
    _relay = None
