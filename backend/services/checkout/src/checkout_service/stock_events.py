"""Checkout's conversation with Inventory, carried on events (spec 008 research R4, R5;
replaces 007's inventory_client).

*reserve* = publish `checkout.stock-reservation-requested`, await the reply event.
*release* = publish `checkout.stock-release-requested`, await `inventory.stock-released`.

Same seam as the HTTP client it replaces: the same result types, so the saga's logic is
unchanged. The shopper-facing `complete` still gets a synchronous answer by awaiting the
reply event for a bounded time.

How a reply finds its waiter: every Checkout worker process runs one reply consumer with no
consumer group, assigned to all partitions of `inventory.stock.v1` and positioned at the end
before the worker reports healthy. The request handler registers an in-process waiter keyed
`(transaction, epoch)` *before* it publishes, so a reply can never be missed. Replies with no
waiter in this process (they belong to another worker, or to an attempt already given up) are
dropped. A reply missed because the worker was down is recovered by re-asking: Inventory
answers a repeated request with its recorded outcome.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Sequence

from checkout_service import eventbus, outbox
from checkout_service.config import settings
from checkout_web.bus import Bus, Transient
from checkout_web.errors import UpstreamUnavailable
from checkout_web.events import (
    CHECKOUT_COMPLETIONS,
    INVENTORY_STOCK,
    RESERVATION_REFUSED,
    STOCK_RELEASE_REQUESTED,
    STOCK_RELEASED,
    STOCK_RESERVATION_REQUESTED,
    STOCK_RESERVED,
    STOCK_REJECTED,
    Event,
    EventDecodeError,
    decode,
    make_event,
)
from checkout_web.request import log_step

log = logging.getLogger("checkout")

PRODUCER = "checkout"
_RESERVE_REPLIES = frozenset({STOCK_RESERVED, STOCK_REJECTED, RESERVATION_REFUSED})
_RELEASE_REPLIES = frozenset({STOCK_RELEASED})
# How the reserve budget is split across the first request and its two re-publishes
# (completion-flow.md "Timing budget": 3 s in all, so a refusal comes within 5 s, SC-009).
_WAIT_SHARES = (0.375, 0.375, 0.25)


@dataclass(frozen=True)
class Applied:
    replayed: bool


@dataclass(frozen=True)
class Insufficient:
    short_skus: list[str]


@dataclass(frozen=True)
class ReservationReleased:
    """Inventory already recorded a release for this transaction and epoch."""


def reservation_request(public_id: str, epoch: int, lines: Sequence[tuple[str, int]]) -> Event:
    return make_event(
        STOCK_RESERVATION_REQUESTED,
        PRODUCER,
        {
            "transactionId": public_id,
            "epoch": epoch,
            "lines": [{"sku": sku, "quantity": qty} for sku, qty in lines],
        },
    )


def release_request(public_id: str, epoch: int) -> Event:
    return make_event(STOCK_RELEASE_REQUESTED, PRODUCER, {"transactionId": public_id, "epoch": epoch})


def _tx_number(public_id: str) -> int:
    return int(public_id.split("-", 1)[1])


class StockEvents:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._waiters: dict[tuple[int, int], list[tuple[frozenset[str], asyncio.Future]]] = {}
        self._consumer = None

    @property
    def ready(self) -> bool:
        return self._consumer is not None and self._consumer.ready

    # ---- reply consumer ----------------------------------------------------------------

    async def run(self) -> None:
        """Read replies forever; resolve the waiter each one belongs to, if it is here."""
        self._consumer = self._bus.consumer(INVENTORY_STOCK, start="end")
        await self._consumer.start()
        while True:
            for rec in await self._consumer.poll(200):
                try:
                    self._dispatch(decode(rec.value))
                except EventDecodeError:
                    continue
                except Exception:
                    log.exception("reply dispatch failed")

    def _dispatch(self, event: Event) -> None:
        data = event.data
        try:
            key = (_tx_number(data["transactionId"]), int(data["epoch"]))
        except (KeyError, ValueError, IndexError):
            return
        for accepts, fut in self._waiters.get(key, ()):
            if event.type in accepts and not fut.done():
                fut.set_result(event)

    # ---- the two asks ------------------------------------------------------------------

    async def reserve(
        self,
        public_id: str,
        epoch: int,
        lines: Sequence[tuple[str, int]],
        event: Event | None = None,
    ) -> Applied | Insufficient | ReservationReleased:
        """Ask Inventory to take the stock. `event` is the request already written to the
        outbox in step 1; without one (a takeover) a fresh request is built. Raises
        UpstreamUnavailable if no reply arrives within the budget."""
        event = event or reservation_request(public_id, epoch, lines)
        waits = [settings.reserve_budget_s * share for share in _WAIT_SHARES]
        reply = await self._ask(public_id, epoch, event, _RESERVE_REPLIES, waits, "reserve")
        if reply.type == STOCK_RESERVED:
            return Applied(replayed=bool(reply.data.get("replayed")))
        if reply.type == STOCK_REJECTED:
            return Insufficient(list(reply.data.get("shortSkus", [])))
        return ReservationReleased()

    async def release(self, public_id: str, epoch: int, event: Event | None = None) -> None:
        """Ask Inventory to undo the reservation. Raises UpstreamUnavailable if the
        release is not confirmed in time (the caller leaves the completion COMPENSATING)."""
        event = event or release_request(public_id, epoch)
        await self._ask(public_id, epoch, event, _RELEASE_REPLIES, [settings.release_wait_s], "release")

    async def _ask(
        self,
        public_id: str,
        epoch: int,
        event: Event,
        accepts: frozenset[str],
        waits: Sequence[float],
        step: str,
    ) -> Event:
        key = (_tx_number(public_id), epoch)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (accepts, fut)
        self._waiters.setdefault(key, []).append(entry)
        t0 = time.monotonic()
        publishes: list[asyncio.Task] = []
        replied = False
        try:
            for attempt, wait in enumerate(waits):
                # The publish runs *inside* the wait, not before it: a broker that is down makes a
                # publish block for its own timeout, and that must not stretch the shopper's
                # deadline (SC-009). Attempt 0 is the outbox fast path (the request is already
                # committed, so a failed publish is the sweeper's to deliver); later attempts
                # re-publish the same request, which Inventory answers from its record.
                if attempt == 0:
                    publishes.append(asyncio.create_task(outbox.publish_now(CHECKOUT_COMPLETIONS, public_id, event)))
                else:
                    publishes.append(asyncio.create_task(self._republish(public_id, event, step, attempt)))
                # asyncio.wait, not wait_for: it neither swallows a cancellation nor cancels
                # the future on timeout, so the same future serves the next, longer wait.
                done, _ = await asyncio.wait({fut}, timeout=wait)
                if not done:
                    continue
                reply = fut.result()
                replied = True  # publishes still finishing (marking the outbox row) are left to finish
                log_step("await", outcome="ok", tx=public_id, reply=reply.type,
                         elapsed_ms=(time.monotonic() - t0) * 1000)
                return reply
            log_step("await", outcome="timeout", tx=public_id, elapsed_ms=(time.monotonic() - t0) * 1000)
            raise UpstreamUnavailable("inventory", f"no reply to the {step} request in time")
        finally:
            if not replied:
                for task in publishes:
                    task.cancel()  # a publish still blocked on a dead broker must not outlive the request
            waiting = self._waiters.get(key, [])
            if entry in waiting:
                waiting.remove(entry)
            if not waiting:
                self._waiters.pop(key, None)

    async def _republish(self, public_id: str, event: Event, step: str, attempt: int) -> None:
        try:
            await self._bus.publish(CHECKOUT_COMPLETIONS, event, key=public_id)
            log_step(step, outcome="republished", tx=public_id, attempt=attempt)
        except Transient:
            pass


_client: StockEvents | None = None


def get_client() -> StockEvents:
    global _client
    if _client is None:
        _client = StockEvents(eventbus.get_bus())
    return _client


def reset() -> None:
    global _client
    _client = None
