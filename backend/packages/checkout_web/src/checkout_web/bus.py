"""The event bus: a small interface with a Kafka and an in-memory implementation
(spec 008 research R1, R13).

Services depend on `Bus`, never on aiokafka, so the same handlers run against real Kafka
in compose and against `MemoryBus` in the in-process test suite. `MemoryBus` keeps the
properties the design relies on (per-key partition order, per-group committed offsets,
redelivery after an uncommitted batch) and adds fault hooks so failure tests are
deterministic: drop, duplicate, delay, reorder, fail a publish, crash before a commit.

Data-free: nothing here knows what an event means.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from checkout_web.events import TOPICS, Event, EventDecodeError, TopicSpec, decode, encode, headers

log = logging.getLogger("checkout.bus")

# Where a consumer starts reading when it has no committed offset:
#   "beginning"  the first retained record
#   "end"        records published after the consumer was created
#   <int n>      the last n records of each partition (a bounded replay)
Start = str | int


class Transient(Exception):
    """A dependency (database or broker) is unreachable. Handlers raise this, and the
    consumer loop waits and retries instead of counting it toward dead-lettering."""


class BrokerUnavailable(Transient):
    """A publish could not be acknowledged by the broker."""


@dataclass(frozen=True)
class Record:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def event(self) -> Event:
        """Decode the body. Raises EventDecodeError for a poison record."""
        return decode(self.value)


class Consumer(Protocol):
    @property
    def ready(self) -> bool:
        """Assigned and positioned: records published from now on will be seen."""

    async def start(self) -> None: ...

    async def poll(self, timeout_ms: int = 500, max_records: int = 500) -> list[Record]: ...

    async def commit(self) -> None:
        """Commit the position after the last polled records (group consumers only)."""

    async def stop(self) -> None: ...


class Bus(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def ready(self) -> bool: ...

    async def publish(self, topic: str, event: Event, key: str | None = None) -> None:
        """Returns once the broker has acknowledged the event. Raises BrokerUnavailable."""

    async def publish_batch(self, topic: str, items: list[tuple[Event, str | None]]) -> None:
        """Publish many events **in the order given** and return once all are acknowledged.
        Raises BrokerUnavailable; some of them may already have been stored."""

    def consumer(
        self, topic: str, *, group: str | None = None, start: Start = "beginning"
    ) -> Consumer: ...


# ======================================================================================
# Kafka
# ======================================================================================


class KafkaBus:
    """aiokafka-backed bus. Starting never blocks the service: the producer connects in
    the background and publishes raise BrokerUnavailable until it has."""

    def __init__(
        self,
        bootstrap: str,
        *,
        client_id: str = "checkout",
        publish_timeout_s: float = 3.0,
    ) -> None:
        self._bootstrap = bootstrap
        self._client_id = client_id
        self._publish_timeout_s = publish_timeout_s
        self._producer: Any = None
        self._connector: asyncio.Task | None = None
        self._consumers: list[KafkaConsumer] = []

    async def start(self) -> None:
        if self._connector is None:
            self._connector = asyncio.create_task(self._connect_forever(), name="kafka-producer")

    async def _connect_forever(self) -> None:
        from aiokafka import AIOKafkaProducer

        delay = 0.5
        while True:
            producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap,
                client_id=self._client_id,
                acks="all",
                # Not idempotent, on purpose. Delivery is at-least-once by design (every consumer
                # is idempotent), and aiokafka's idempotent producer wedges for good with
                # OutOfOrderSequenceNumber after the broker restarts under load (found by
                # scripts/check_broker_outage.sh). `_reset_producer` is the safety net.
                enable_idempotence=False,
                linger_ms=2,
                request_timeout_ms=5000,
            )
            try:
                await producer.start()
            except asyncio.CancelledError:
                await _quiet(producer.stop())
                raise
            except Exception as exc:
                log.warning("kafka producer not connected yet: %s", exc)
                await _quiet(producer.stop())
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            self._producer = producer
            log.info("kafka producer connected to %s", self._bootstrap)
            return

    def ready(self) -> bool:
        return self._producer is not None

    # Errors after which a producer can never send again: it must be replaced.
    _FATAL = frozenset({
        "OutOfOrderSequenceNumber", "DuplicateSequenceNumber", "UnknownProducerId",
        "InvalidProducerEpoch", "InvalidProducerIdMapping", "ProducerFenced", "ProducerClosed",
    })

    def _reset_if_fatal(self, exc: BaseException) -> None:
        if type(exc).__name__ in self._FATAL:
            self._reset_producer(exc)

    def _reset_producer(self, why: BaseException) -> None:
        """Throw the producer away and connect a fresh one (in the background)."""
        old, self._producer = self._producer, None
        if old is None or self._connector is None:
            return
        log.warning("replacing the kafka producer after %r", why)
        asyncio.create_task(_quiet(old.stop()))
        self._connector = asyncio.create_task(self._connect_forever(), name="kafka-producer")

    async def publish(self, topic: str, event: Event, key: str | None = None) -> None:
        producer = self._producer
        if producer is None:
            raise BrokerUnavailable("the broker connection is not up yet")
        hdrs = [(k, v.encode()) for k, v in headers(event).items()]
        try:
            await asyncio.wait_for(
                producer.send_and_wait(
                    topic,
                    encode(event),
                    key=key.encode() if key is not None else None,
                    headers=hdrs,
                ),
                self._publish_timeout_s,
            )
        except Exception as exc:  # KafkaError, timeout, connection loss
            self._reset_if_fatal(exc)
            raise BrokerUnavailable(f"publish to {topic} failed: {exc!r}") from exc

    async def publish_batch(self, topic: str, items: list[tuple[Event, str | None]]) -> None:
        producer = self._producer
        if producer is None:
            raise BrokerUnavailable("the broker connection is not up yet")
        try:
            # `send` enqueues in call order (so partition order is call order); the acks are
            # awaited together, so the producer batches the whole run into a few requests.
            futures = []
            for event, key in items:
                futures.append(
                    await producer.send(
                        topic,
                        encode(event),
                        key=key.encode() if key is not None else None,
                        headers=[(k, v.encode()) for k, v in headers(event).items()],
                    )
                )
            await asyncio.wait_for(asyncio.gather(*futures), self._publish_timeout_s + len(items) / 500)
        except Exception as exc:
            self._reset_if_fatal(exc)
            raise BrokerUnavailable(f"batch publish to {topic} failed: {exc!r}") from exc

    def consumer(
        self, topic: str, *, group: str | None = None, start: Start = "beginning"
    ) -> "KafkaConsumer":
        c = KafkaConsumer(self._bootstrap, topic, group=group, start=start, client_id=self._client_id)
        self._consumers.append(c)
        return c

    async def stop(self) -> None:
        if self._connector is not None:
            self._connector.cancel()
            await _quiet(self._connector)
            self._connector = None
        for c in self._consumers:
            await c.stop()
        self._consumers.clear()
        if self._producer is not None:
            await _quiet(self._producer.stop())
            self._producer = None


class KafkaConsumer:
    """A group consumer (shared work, committed offsets) when `group` is given, otherwise
    an unshared reader assigned to every partition of the topic. Connects lazily on the
    first poll, so a service can start before the broker."""

    def __init__(
        self, bootstrap: str, topic: str, *, group: str | None, start: Start, client_id: str
    ) -> None:
        self._bootstrap = bootstrap
        self._topic = topic
        self._group = group
        self._start = start
        self._client_id = client_id
        self._c: Any = None
        self._positioned = False

    @property
    def ready(self) -> bool:
        if self._c is None:
            return False
        if self._group is None:
            return self._positioned
        return bool(self._c.assignment())

    async def start(self) -> None:
        await self._ensure_started()

    async def _ensure_started(self) -> bool:
        if self._c is not None:
            return True
        from aiokafka import AIOKafkaConsumer, TopicPartition

        topics = (self._topic,) if self._group is not None else ()
        consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            group_id=self._group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            request_timeout_ms=10000,
            session_timeout_ms=10000,
            heartbeat_interval_ms=3000,
        )
        try:
            await consumer.start()
            if self._group is None:
                partitions = consumer.partitions_for_topic(self._topic)
                if not partitions:
                    await consumer._client.force_metadata_update()
                    partitions = consumer.partitions_for_topic(self._topic)
                if not partitions:
                    raise RuntimeError(f"topic {self._topic} does not exist yet")
                tps = [TopicPartition(self._topic, p) for p in sorted(partitions)]
                consumer.assign(tps)
                if self._start == "beginning":
                    await consumer.seek_to_beginning(*tps)
                elif self._start == "end":
                    await consumer.seek_to_end(*tps)
                else:
                    n = int(self._start)
                    ends = await consumer.end_offsets(tps)
                    begins = await consumer.beginning_offsets(tps)
                    for tp in tps:
                        consumer.seek(tp, max(begins[tp], ends[tp] - n))
                for tp in tps:  # force the lazy seek to resolve before anyone relies on it
                    await consumer.position(tp)
                self._positioned = True
        except asyncio.CancelledError:
            await _quiet(consumer.stop())
            raise
        except Exception as exc:
            log.warning("kafka consumer for %s not started yet: %s", self._topic, exc)
            await _quiet(consumer.stop())
            return False
        self._c = consumer
        return True

    async def poll(self, timeout_ms: int = 500, max_records: int = 500) -> list[Record]:
        if not await self._ensure_started():
            await asyncio.sleep(1.0)
            return []
        try:
            batches = await self._c.getmany(timeout_ms=timeout_ms, max_records=max_records)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("kafka poll on %s failed: %s", self._topic, exc)
            await asyncio.sleep(1.0)
            return []
        out: list[Record] = []
        for tp, msgs in batches.items():
            for m in msgs:
                out.append(
                    Record(
                        topic=tp.topic,
                        partition=tp.partition,
                        offset=m.offset,
                        key=m.key.decode() if m.key is not None else None,
                        value=m.value,
                        headers={k: v.decode() for k, v in (m.headers or [])},
                    )
                )
        return out

    async def commit(self) -> None:
        if self._group is None or self._c is None:
            return
        try:
            await self._c.commit()
        except Exception as exc:  # e.g. a rebalance: the batch is redelivered, handlers are idempotent
            log.warning("offset commit on %s failed (batch will be redelivered): %s", self._topic, exc)

    async def stop(self) -> None:
        if self._c is not None:
            await _quiet(self._c.stop())
            self._c = None
            self._positioned = False


async def _quiet(awaitable) -> None:
    try:
        await awaitable
    except BaseException:  # noqa: BLE001 - shutdown paths must not raise
        pass


# ======================================================================================
# In memory
# ======================================================================================

Predicate = Callable[[str, Event, "str | None"], bool]


def _always(_topic: str, _event: Event, _key: str | None) -> bool:
    return True


@dataclass
class _Fault:
    kind: str  # fail | drop | duplicate | delay | reorder
    predicate: Predicate
    times: int
    arg: float = 0.0
    held: tuple | None = None  # reorder: the record being held back

    def matches(self, topic: str, event: Event, key: str | None) -> bool:
        return self.times > 0 and self.predicate(topic, event, key)


class MemoryBus:
    """In-process bus with the same semantics the services rely on, plus fault hooks."""

    def __init__(self, topics: dict[str, TopicSpec] | None = None) -> None:
        self._topics = dict(topics if topics is not None else TOPICS)
        self._logs: dict[str, list[list[Record]]] = {
            t: [[] for _ in range(spec.partitions)] for t, spec in self._topics.items()
        }
        self._committed: dict[tuple[str, str], dict[int, int]] = {}
        self._faults: list[_Fault] = []
        self._crash_before_commit: dict[str, int] = {}
        self._waiters: list[asyncio.Future] = []  # consumers waiting for a new record
        self._rr = itertools.count()
        self._pending: set[asyncio.Task] = set()
        self.published: list[tuple[str, str | None, Event]] = []  # every accepted publish, in order
        self.attempts: list[tuple[str, str | None, Event]] = []  # every publish call, accepted or not

    # ---- Bus -----------------------------------------------------------------------

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        for task in list(self._pending):
            task.cancel()

    def ready(self) -> bool:
        return True

    async def publish(self, topic: str, event: Event, key: str | None = None) -> None:
        if topic not in self._logs:
            raise BrokerUnavailable(f"unknown topic {topic}")
        self.attempts.append((topic, key, event))
        for fault in self._faults:
            if fault.kind == "fail" and fault.matches(topic, event, key):
                fault.times -= 1
                raise BrokerUnavailable("injected publish failure")
        copies = 1
        for fault in self._faults:
            if fault.kind == "drop" and fault.matches(topic, event, key):
                fault.times -= 1
                return
            if fault.kind == "duplicate" and fault.matches(topic, event, key):
                fault.times -= 1
                copies += int(fault.arg)
            if fault.kind == "delay" and fault.matches(topic, event, key):
                fault.times -= 1
                task = asyncio.create_task(self._append_later(topic, event, key, fault.arg, copies))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
                return
            if fault.kind == "reorder" and fault.matches(topic, event, key):
                if fault.held is None:
                    fault.held = (topic, event, key, copies)
                    return  # held until the next matching publish
                held, fault.held = fault.held, None
                fault.times -= 1
                await self._append(topic, event, key, copies)
                await self._append(*held[:3], held[3])
                return
        await self._append(topic, event, key, copies)

    async def publish_batch(self, topic: str, items: list[tuple[Event, str | None]]) -> None:
        for event, key in items:
            await self.publish(topic, event, key)

    async def publish_raw(self, topic: str, value: bytes, key: str | None = None) -> None:
        """Store bytes that need not be a valid event (tests: a poison record)."""
        part = self._partition_for(topic, key)
        log_ = self._logs[topic][part]
        log_.append(Record(topic=topic, partition=part, offset=len(log_), key=key, value=value))
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)

    def consumer(
        self, topic: str, *, group: str | None = None, start: Start = "beginning"
    ) -> "MemoryConsumer":
        return MemoryConsumer(self, topic, group, start)

    # ---- fault hooks (tests) -------------------------------------------------------

    def fail_publish(self, times: int = 1, predicate: Predicate = _always) -> None:
        """The next `times` matching publishes raise BrokerUnavailable (nothing stored)."""
        self._faults.append(_Fault("fail", predicate, times))

    def drop(self, predicate: Predicate, times: int = 1) -> None:
        """The next `times` matching publishes are acknowledged but never stored."""
        self._faults.append(_Fault("drop", predicate, times))

    def duplicate(self, predicate: Predicate = _always, *, copies: int = 1, times: int = 1_000_000) -> None:
        """Matching events are stored `1 + copies` times (at-least-once redelivery)."""
        self._faults.append(_Fault("duplicate", predicate, times, arg=float(copies)))

    def delay(self, predicate: Predicate, seconds: float, times: int = 1) -> None:
        """Matching events are stored `seconds` later (the publish returns at once)."""
        self._faults.append(_Fault("delay", predicate, times, arg=seconds))

    def reorder(self, predicate: Predicate, times: int = 1) -> None:
        """Hold the next matching event and store it after the one that follows it."""
        self._faults.append(_Fault("reorder", predicate, times))

    def crash_before_commit(self, group: str, times: int = 1) -> None:
        """The group's next `times` commits are lost: the batch is redelivered, as after a
        crash between handling and committing."""
        self._crash_before_commit[group] = self._crash_before_commit.get(group, 0) + times

    def clear_faults(self) -> None:
        self._faults.clear()
        self._crash_before_commit.clear()

    # ---- inspection (tests) --------------------------------------------------------

    def records(self, topic: str) -> list[Record]:
        out = [r for part in self._logs[topic] for r in part]
        return sorted(out, key=lambda r: (r.partition, r.offset))

    def events(self, topic: str) -> list[Event]:
        return [r.event() for r in self.records(topic)]

    def attempts_of(self, *types: str) -> int:
        """How many times an event of these types was handed to `publish` (accepted or not)."""
        return sum(1 for _, _, e in self.attempts if e.type in types)

    def end_offsets(self, topic: str) -> list[int]:
        return [len(p) for p in self._logs[topic]]

    # ---- internals -----------------------------------------------------------------

    def _partition_for(self, topic: str, key: str | None) -> int:
        n = len(self._logs[topic])
        if key is None:
            return next(self._rr) % n
        return zlib.crc32(key.encode()) % n

    async def _append(self, topic: str, event: Event, key: str | None, copies: int = 1) -> None:
        part = self._partition_for(topic, key)
        for _ in range(copies):
            log_ = self._logs[topic][part]
            log_.append(
                Record(
                    topic=topic,
                    partition=part,
                    offset=len(log_),
                    key=key,
                    value=encode(event),
                    headers=headers(event),
                )
            )
        self.published.append((topic, key, event))
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)

    async def _append_later(self, topic, event, key, seconds, copies) -> None:
        await asyncio.sleep(seconds)
        await self._append(topic, event, key, copies)


class MemoryConsumer:
    def __init__(self, bus: MemoryBus, topic: str, group: str | None, start: Start) -> None:
        self._bus = bus
        self._topic = topic
        self._group = group
        parts = len(bus._logs[topic])
        committed = bus._committed.get((group, topic), {}) if group else {}
        self._pos: dict[int, int] = {}
        for p in range(parts):
            size = len(bus._logs[topic][p])
            if group and p in committed:
                self._pos[p] = committed[p]
            elif start == "beginning":
                self._pos[p] = 0
            elif start == "end":
                self._pos[p] = size
            else:
                self._pos[p] = max(0, size - int(start))

    @property
    def ready(self) -> bool:
        return True

    async def start(self) -> None:
        return None

    async def poll(self, timeout_ms: int = 500, max_records: int = 500) -> list[Record]:
        out = self._take(max_records)
        if out:
            return out
        # asyncio.wait, not wait_for: wait_for can swallow a cancellation (see lifecycle.py).
        fut = asyncio.get_running_loop().create_future()
        self._bus._waiters.append(fut)
        try:
            await asyncio.wait({fut}, timeout=timeout_ms / 1000)
        finally:
            self._bus._waiters.remove(fut)
        return self._take(max_records)

    def _take(self, max_records: int) -> list[Record]:
        out: list[Record] = []
        for p, log_ in enumerate(self._bus._logs[self._topic]):
            start = self._pos[p]
            chunk = log_[start : start + max(0, max_records - len(out))]
            out.extend(chunk)
            self._pos[p] = start + len(chunk)
        return out

    async def commit(self) -> None:
        if self._group is None:
            return
        left = self._bus._crash_before_commit.get(self._group, 0)
        if left > 0:
            self._bus._crash_before_commit[self._group] = left - 1
            committed = self._bus._committed.get((self._group, self._topic), {})
            for p in self._pos:  # as after a restart: resume at the last commit
                self._pos[p] = committed.get(p, 0)
            return
        self._bus._committed[(self._group, self._topic)] = dict(self._pos)

    async def stop(self) -> None:
        return None


__all__ = [
    "Bus",
    "BrokerUnavailable",
    "Consumer",
    "EventDecodeError",
    "KafkaBus",
    "MemoryBus",
    "Record",
    "Start",
    "Transient",
]
