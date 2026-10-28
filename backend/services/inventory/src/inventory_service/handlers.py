"""Inventory's side of the completion flow: consume reservation and release requests,
apply them, answer with an outcome event (spec 008 research R6;
specs/008-event-driven-kafka/contracts/completion-flow.md).

Each request is handled in **one local transaction** that changes stock (unchanged 007
statements in reserve.py) and writes the outcome into the outbox. After COMMIT the outcome
is published, then the consumer loop commits the offset. So:

* a crash before COMMIT: nothing happened, the request is redelivered and handled once;
* a crash after COMMIT, before publish: the sweeper publishes the outbox row;
* a crash after publish, before the offset commit: the request is redelivered, finds its
  recorded adjustment, and re-emits the same outcome. Stock is never applied twice.

The hot-SKU row locks live only inside the statement plus COMMIT (spec 006): the Kafka I/O
happens after they are released.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError

from checkout_web.bus import Bus, Record, Transient
from checkout_web.consumer_loop import run_consumer
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
    make_event,
)
from checkout_web.request import log_step
from inventory_service import outbox, repo
from inventory_service import reserve as reservations
from inventory_service.config import settings
from inventory_service.db import SessionLocal

log = logging.getLogger("inventory")

PRODUCER = "inventory"
GROUP = "inventory"
CONCURRENCY = 6  # below the connection pool, leaving room for the report endpoint

_consumer = None


def ready() -> bool:
    return _consumer is not None and _consumer.ready


def _tx_number(public_id: str) -> int:
    return int(public_id.split("-", 1)[1])


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, (OperationalError, InterfaceError, OSError, asyncio.TimeoutError)):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


def _outcome(request: Event, etype: str, **extra) -> Event:
    data = request.data
    return make_event(
        etype,
        PRODUCER,
        {"transactionId": data["transactionId"], "epoch": int(data["epoch"]),
         "inReplyTo": request.event_id, **extra},
        correlation_id=request.correlation_id,
    )


async def handle_reservation(request: Event) -> Event:
    """Reserve (or replay, or refuse) and queue the outcome, in one local transaction."""
    data = request.data
    public, epoch = data["transactionId"], int(data["epoch"])
    lines = [(line["sku"], int(line["quantity"])) for line in data["lines"]]
    for attempt in (1, 2):
        try:
            async with SessionLocal() as session:
                async with session.begin():
                    result = await reservations.reserve(
                        session, _tx_number(public), lines, settings.low_stock_threshold, epoch=epoch
                    )
                    if isinstance(result, reservations.Reserved):
                        outcome = _outcome(request, STOCK_RESERVED, replayed=result.replayed)
                    elif isinstance(result, reservations.Short):
                        outcome = _outcome(request, STOCK_REJECTED, shortSkus=result.skus)
                    else:
                        outcome = _outcome(request, RESERVATION_REFUSED)
                    await repo.insert_outbox(session, INVENTORY_STOCK, public, outcome)
            return outcome
        except IntegrityError:
            # An identical request committed first. Run again: the statement now finds its
            # record and replays it.
            if attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


async def handle_release(request: Event) -> Event:
    """Undo (or record that none may follow) and queue the outcome, in one local transaction."""
    data = request.data
    public, epoch = data["transactionId"], int(data["epoch"])
    async with SessionLocal() as session:
        async with session.begin():
            result = await reservations.release(session, _tx_number(public), epoch=epoch)
            outcome = _outcome(
                request,
                STOCK_RELEASED,
                unitsRestored=result.units_restored,
                alreadyReleased=result.already_released,
            )
            await repo.insert_outbox(session, INVENTORY_STOCK, public, outcome)
    return outcome


async def handle(rec: Record) -> None:
    """One request event. Raises Transient when the database is unreachable, so the
    consumer loop waits instead of dead-lettering."""
    request = rec.event()  # EventDecodeError -> dead letter
    started = time.monotonic()
    if request.type == STOCK_RESERVATION_REQUESTED:
        step, work = "reserve", handle_reservation
    elif request.type == STOCK_RELEASE_REQUESTED:
        step, work = "release", handle_release
    else:
        return  # unknown types are ignored (compatibility rule)
    try:
        outcome = await work(request)
    except Exception as exc:
        if _transient(exc):
            raise Transient(f"inventory database unavailable: {exc!r}") from exc
        raise
    log_step(step, outcome=outcome.type.split(".", 1)[1], tx=request.data["transactionId"],
             elapsed_ms=(time.monotonic() - started) * 1000)
    await outbox.publish_now(INVENTORY_STOCK, request.data["transactionId"], outcome)


async def run(bus: Bus) -> None:
    """Consume `checkout.completions.v1` in the shared group `inventory`."""
    global _consumer
    _consumer = bus.consumer(CHECKOUT_COMPLETIONS, group=GROUP)
    await run_consumer(_consumer, handle, name=GROUP, bus=bus, concurrency=CONCURRENCY)
