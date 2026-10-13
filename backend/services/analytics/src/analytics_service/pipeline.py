"""Persistent streaming pipeline for popular-items analytics.

One pipeline runs for the life of the Analytics leader (specs/005-service-
architecture/research.md R5). Five filters, connected by bounded queues, run
concurrently and keep their state across windows:

    TailIngest ─q1─▶ WindowFilter ─q2─▶ AggregationFilter ─q3─▶ RankingFilter ─slot─▶ OutputFilter
    (DB tail)        (slide by id)      (running counts)        (top-200)              (latest-wins, commit)

TailIngest follows the shared scan record (`transaction_item.id` is the global scan
sequence) as rows commit; nothing notifies it. The window is defined by scan id:
it slides on each multiple of the slide interval, once TailIngest has accounted
for every id at or below that boundary (R6). TERMINATE flows downstream only at
shutdown, never between windows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import orjson

from checkout_data.config import settings
from checkout_data.repos import analytics as analytics_repo

log = logging.getLogger("analytics")

WINDOW_SIZE = settings.popular_window_size
SLIDE_INTERVAL = settings.popular_slide_interval
# Top 200 covers any sane ?limit=; names and rank are attached at read time.
RANKING_DEPTH = 200

# Sentinel for pipeline shutdown.
TERMINATE = object()


@dataclass(frozen=True)
class ScanData:
    """One committed scan. scan_id is transaction_item.id."""

    scan_id: int
    sku: str


@dataclass(frozen=True)
class Watermark:
    """Every scan id <= boundary has been emitted or declared a permanent gap."""

    boundary: int


@dataclass(frozen=True)
class Slide:
    """The window now covers (window_start, window_end]; SKUs entering and leaving it."""

    window_start: int
    window_end: int
    added: list[str]
    evicted: list[str]


@dataclass(frozen=True)
class WindowCounts:
    """Per-SKU scan counts for the window (window_start, window_end]."""

    window_start: int
    window_end: int
    counts: dict[str, int]


@dataclass(frozen=True)
class RankedData:
    """A ranked window, ready for persistence. computed_at None means DB now()."""

    window_size: int
    slide_interval: int
    window_start: int
    window_end: int
    computed_at: datetime | None
    ranking: list[dict[str, Any]]


def first_boundary_after(start_after: int, slide: int) -> int:
    """The smallest multiple of `slide` strictly greater than `start_after`."""
    return (start_after // slide + 1) * slide


class TailIngest:
    """Tails `transaction_item` from `start_after`, emitting ScanData in id order.

    Keeps a contiguous watermark `last_id`: rows above a hole are held in
    `pending` until the hole fills. A hole that stays open longer than the grace
    period is declared a permanent gap (a sequence value that never committed),
    logged, and skipped. After each id it accepts, it emits Watermark(b) for
    every slide boundary b it has passed (R6).
    """

    def __init__(
        self,
        session_factory,
        start_after: int,
        *,
        repo=analytics_repo,
        first_boundary: int | None = None,
        slide: int = SLIDE_INTERVAL,
        batch: int = 1000,
        poll_s: float | None = None,
        grace_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._session_factory = session_factory
        self._repo = repo
        self._slide = slide
        self._batch = batch
        self._poll_s = settings.popular_poll_ms / 1000 if poll_s is None else poll_s
        self._grace_s = settings.popular_gap_grace_ms / 1000 if grace_s is None else grace_s
        self._clock = clock
        self._sleep = sleep

        self.last_id = start_after
        self.next_boundary = (
            first_boundary if first_boundary is not None else first_boundary_after(start_after, slide)
        )
        self.pending: dict[int, str] = {}
        self._hole_since: float | None = None
        self._hole_upto = 0

    async def _advance(self, scan_id: int, out: asyncio.Queue) -> None:
        self.last_id = scan_id
        while self.next_boundary <= self.last_id:
            await out.put(Watermark(self.next_boundary))
            self.next_boundary += self._slide

    async def _drain(self, out: asyncio.Queue) -> bool:
        """Emit pending rows contiguous with last_id. True if any were emitted."""
        progressed = False
        while (sku := self.pending.pop(self.last_id + 1, None)) is not None:
            await out.put(ScanData(self.last_id + 1, sku))
            await self._advance(self.last_id + 1, out)
            progressed = True
        return progressed

    async def _skip_expired_hole(self, out: asyncio.Queue) -> bool:
        """Handle a hole below the lowest pending id. True if ids were skipped."""
        if not self.pending:
            self._hole_since = None
            return False
        now = self._clock()
        if self._hole_since is None:
            # Every id below the lowest pending row is already "late" as of now.
            self._hole_since, self._hole_upto = now, min(self.pending) - 1
            return False
        if now - self._hole_since <= self._grace_s:
            return False
        for missing in range(self.last_id + 1, self._hole_upto + 1):
            log.info("gap skipped id=%d", missing)
            await self._advance(missing, out)
        self._hole_since = None
        return True

    async def run(self, out: asyncio.Queue) -> None:
        while True:
            async with self._session_factory() as session:
                rows = await self._repo.get_scans_after(session, self.last_id, self._batch)
            for row in rows:
                if row.id > self.last_id:
                    self.pending[row.id] = row.sku

            progressed = await self._drain(out)
            if progressed:
                self._hole_since = None
            if await self._skip_expired_hole(out):
                await self._drain(out)
                continue
            # Keep reading at DB speed while a backlog drains; sleep when caught
            # up or when stuck behind a hole (re-reading would only spin).
            if len(rows) < self._batch or not progressed:
                await self._sleep(self._poll_s)


class WindowFilter:
    """Maintains the scan window by id; emits a Slide on each Watermark."""

    def __init__(self, start_after: int = 0, *, window: int = WINDOW_SIZE, slide: int = SLIDE_INTERVAL):
        self.buffer: deque[ScanData] = deque()
        self.window = window
        self.slide = slide
        # Scans with id <= last_boundary have already been counted downstream.
        self.last_boundary = start_after

    async def run(self, inq: asyncio.Queue, outq: asyncio.Queue) -> None:
        while True:
            item = await inq.get()
            if item is TERMINATE:
                await outq.put(TERMINATE)
                return
            if isinstance(item, ScanData):
                self.buffer.append(item)
                continue

            b = item.boundary
            added = [s.sku for s in self.buffer if self.last_boundary < s.scan_id <= b]
            evicted = []
            while self.buffer and self.buffer[0].scan_id <= b - self.window:
                evicted.append(self.buffer.popleft().sku)
            self.last_boundary = b
            await outq.put(Slide(max(0, b - self.window), b, added, evicted))


class AggregationFilter:
    """One running per-SKU Counter, updated incrementally by each Slide."""

    def __init__(self):
        self.counts: Counter[str] = Counter()

    async def run(self, inq: asyncio.Queue, outq: asyncio.Queue) -> None:
        while True:
            item = await inq.get()
            if item is TERMINATE:
                await outq.put(TERMINATE)
                return
            self.counts.update(item.added)
            self.counts.subtract(item.evicted)
            for sku in [s for s, c in self.counts.items() if c <= 0]:
                del self.counts[sku]
            await outq.put(WindowCounts(item.window_start, item.window_end, dict(self.counts)))


class RankingFilter:
    """Ranks a window by scan count, ties by SKU ascending (spec FR-012b)."""

    def __init__(self, depth: int = RANKING_DEPTH, *, window: int = WINDOW_SIZE, slide: int = SLIDE_INTERVAL):
        self.depth = depth
        self.window = window
        self.slide = slide

    async def run(self, inq: asyncio.Queue, out) -> None:
        while True:
            item = await inq.get()
            if item is TERMINATE:
                await out.put(TERMINATE)
                return
            top = sorted(item.counts.items(), key=lambda kv: (-kv[1], kv[0]))[: self.depth]
            await out.put(
                RankedData(
                    window_size=self.window,
                    slide_interval=self.slide,
                    window_start=item.window_start,
                    window_end=item.window_end,
                    computed_at=None,
                    ranking=[{"sku": sku, "scanCount": n} for sku, n in top],
                )
            )


class LatestSlot:
    """A one-item mailbox: a newer put replaces an unconsumed older item.

    Clients only ever see the latest snapshot, so a burst of slides costs one
    write, not a backlog. TERMINATE is kept separately and returned only after
    the pending item, so shutdown never drops the final window.
    """

    def __init__(self):
        self._item: Any = None
        self._closed = False
        self._ready = asyncio.Event()

    async def put(self, item: Any) -> None:
        if item is TERMINATE:
            self._closed = True
        else:
            self._item = item
        self._ready.set()

    async def get(self) -> Any:
        while self._item is None and not self._closed:
            self._ready.clear()
            await self._ready.wait()
        item, self._item = self._item, None
        return TERMINATE if item is None else item


class OutputFilter:
    """Persists each ranked window (upsert + commit), newest first, never older."""

    def __init__(self, session_factory, *, repo=analytics_repo, on_publish: Callable[[RankedData], None] | None = None):
        self._session_factory = session_factory
        self._repo = repo
        self._on_publish = on_publish
        self.last_written_end = 0

    async def run(self, slot: LatestSlot) -> None:
        while True:
            data = await slot.get()
            if data is TERMINATE:
                return
            if data.window_end <= self.last_written_end:
                continue
            async with self._session_factory() as session:
                await self._repo.upsert_snapshot(
                    session,
                    data.window_size,
                    data.slide_interval,
                    data.window_start,
                    data.window_end,
                    orjson.dumps(data.ranking).decode(),
                    data.computed_at,
                )
                await session.commit()
            self.last_written_end = data.window_end
            log.info("window published start=%d end=%d", data.window_start, data.window_end)
            if self._on_publish is not None:
                self._on_publish(data)
