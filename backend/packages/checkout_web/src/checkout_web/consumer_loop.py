"""The consumer loop every event consumer runs (spec 008 research R6, R9).

    poll a batch -> handle each record -> commit offsets only after the whole batch is done

Records that share a (partition, key) are handled one after another, in order; different
keys run concurrently up to `concurrency`, so one slow transaction does not hold up the
rest. A handler that raises is retried with backoff; after `max_attempts` the record is
published to the dead-letter topic and the loop moves on, so a poison event never blocks a
partition. A handler that raises `Transient` (database or broker unreachable) is retried
without limit and without dead-lettering: an outage must not discard good events.

At-least-once by design: if the process dies after handling and before the commit, the
batch is redelivered. Every handler is idempotent (contracts/events.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

import orjson

from checkout_web.bus import Bus, BrokerUnavailable, Consumer, Record, Transient
from checkout_web.events import DEAD_LETTER, DEAD_LETTERS, EventDecodeError, make_event
from checkout_web.request import _request_id, log_step, new_request_id

log = logging.getLogger("checkout.consumer")

Handler = Callable[[Record], Awaitable[None]]


async def run_consumer(
    consumer: Consumer,
    handler: Handler,
    *,
    name: str,
    bus: Bus,
    concurrency: int = 8,
    max_attempts: int = 5,
    backoff_s: float = 0.2,
    transient_backoff_s: float = 0.5,
    poll_ms: int = 500,
) -> None:
    """Run until cancelled. `name` identifies this consumer in dead letters and logs."""
    sem = asyncio.Semaphore(max(1, concurrency))
    while True:
        records = await consumer.poll(poll_ms)
        if not records:
            continue
        groups: dict[tuple[int, str | None], list[Record]] = defaultdict(list)
        for rec in records:
            groups[(rec.partition, rec.key)].append(rec)

        async def run_group(group: list[Record]) -> None:
            async with sem:
                for rec in group:
                    await _handle_one(
                        rec, handler, name, bus, max_attempts, backoff_s, transient_backoff_s
                    )

        await asyncio.gather(*(run_group(g) for g in groups.values()))
        await consumer.commit()


async def _handle_one(
    rec: Record,
    handler: Handler,
    name: str,
    bus: Bus,
    max_attempts: int,
    backoff_s: float,
    transient_backoff_s: float,
) -> None:
    attempt = 0
    made = 0  # attempts actually made, reported in the dead letter
    last: BaseException | None = None
    while attempt < max_attempts:
        token = _request_id.set(rec.headers.get("x-request-id") or new_request_id())
        try:
            await handler(rec)
            return
        except asyncio.CancelledError:
            raise
        except Transient as exc:
            log_step("consume", outcome="transient", consumer=name, topic=rec.topic,
                     partition=rec.partition, offset=rec.offset, error=repr(exc))
            await asyncio.sleep(transient_backoff_s)
            continue  # not counted: wait for the dependency to return
        except EventDecodeError as exc:
            last = exc
            made = 1
            attempt = max_attempts  # a malformed event cannot succeed on retry
            break
        except Exception as exc:  # noqa: BLE001 - the loop must survive any handler bug
            last = exc
            attempt += 1
            made = attempt
            log_step("consume", outcome="retry", consumer=name, topic=rec.topic,
                     partition=rec.partition, offset=rec.offset, attempt=attempt, error=repr(exc))
            if attempt < max_attempts:
                await asyncio.sleep(backoff_s * (2 ** (attempt - 1)))
        finally:
            _request_id.reset(token)
    await _dead_letter(rec, name, bus, made, last)


def _raw(value: bytes):
    try:
        return orjson.loads(value)
    except Exception:
        return value.decode("utf-8", errors="replace")


async def _dead_letter(rec: Record, name: str, bus: Bus, attempts: int, error: BaseException | None) -> None:
    event = make_event(
        DEAD_LETTER,
        name,
        {
            "sourceTopic": rec.topic,
            "partition": rec.partition,
            "offset": rec.offset,
            "consumer": name,
            "attempts": max(1, attempts),
            "error": repr(error) if error else "",
            "event": _raw(rec.value),
        },
        correlation_id=rec.headers.get("x-request-id"),
    )
    while True:
        try:
            await bus.publish(DEAD_LETTERS, event, key=rec.topic)
            break
        except BrokerUnavailable:
            await asyncio.sleep(0.5)
    log_step("consume", outcome="dead-lettered", consumer=name, topic=rec.topic,
             partition=rec.partition, offset=rec.offset, attempts=attempts)
