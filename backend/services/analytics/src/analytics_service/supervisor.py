"""Owns the one persistent pipeline: bootstrap, restart, shutdown.

Lifecycle (specs/007-microservices-architecture/data-model.md §4):
  bootstrapping — read Checkout's highest scan id, tail from b0 - window so the first
                  window published is complete
  streaming     — five filters run until shutdown; windows are published as they slide
  restarting    — any filter error, or Checkout's data being reset: cancel all filters,
                  back off 1 s, bootstrap again

There is no leader election. 005 used a database advisory lock so that replicas would not
run duplicate pipelines writing one shared snapshot row; with state held in memory there
is no shared write target, and exactly one instance runs. If Checkout is unreachable
while bootstrapping, the supervisor retries every `backoff_s` and the endpoint answers
with whatever was last published (or the empty window).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Callable

from analytics_service import state
from analytics_service.pipeline import (
    SLIDE_INTERVAL,
    TERMINATE,
    WINDOW_SIZE,
    AggregationFilter,
    CursorAhead,
    LatestSlot,
    OutputFilter,
    RankedData,
    RankingFilter,
    TailIngest,
    WindowFilter,
)
from analytics_service.scan_source import ScanSource, default_source
from checkout_web.errors import UpstreamUnavailable

log = logging.getLogger("analytics")


def _publish(data: RankedData) -> None:
    state.latest = data


class PipelineSupervisor:
    def __init__(
        self,
        source: ScanSource | None = None,
        on_publish: Callable[[RankedData], None] | None = None,
        *,
        backoff_s: float = 1.0,
        drain_s: float = 2.0,
        poll_s: float | None = None,
        grace_s: float | None = None,
    ):
        self._source = source if source is not None else default_source()
        self._on_publish = on_publish
        self._backoff_s = backoff_s
        self._drain_s = drain_s
        self._poll_s = poll_s
        self._grace_s = grace_s
        self.instances = 0  # pipelines built; read by tests (one per bootstrap)
        self._tail_task: asyncio.Task | None = None
        self._q1: asyncio.Queue | None = None
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self._run_pipeline()
                    return  # drained after stop()
                except asyncio.CancelledError:
                    raise
                except UpstreamUnavailable as exc:
                    log.warning("pipeline waiting for checkout: %s", exc)
                    await asyncio.sleep(self._backoff_s)
                except CursorAhead as exc:
                    # Not a failure: a new scan log is simply read from the start, at once.
                    log.warning("checkout data was reset; starting over: %s", exc)
                    state.reset()
                except Exception:
                    log.exception("pipeline restarting after error")
                    await asyncio.sleep(self._backoff_s)
        finally:
            await self._cancel_filters()

    async def _run_pipeline(self) -> None:
        max_seq = await self._source.max_scan_id()
        b0 = max_seq - max_seq % SLIDE_INTERVAL
        start_after = max(0, b0 - WINDOW_SIZE)
        # The first window published is b0 itself (complete), never a half-filled
        # earlier boundary; below the first boundary the read path answers on demand.
        first_boundary = b0 if b0 > 0 else SLIDE_INTERVAL

        # Rebuild the in-memory window from Checkout. The last published ranking is kept
        # until the rebuilt pipeline publishes a newer one.
        state.window.clear()
        state.cursor = start_after

        self.instances += 1
        log.info("pipeline started bootstrap_from=%d", start_after)

        def advance(last_id: int) -> None:
            state.cursor = last_id

        def publish(data: RankedData) -> None:
            _publish(data)
            if self._on_publish is not None:
                self._on_publish(data)

        q1: asyncio.Queue = asyncio.Queue(maxsize=10000)
        q2: asyncio.Queue = asyncio.Queue(maxsize=64)
        q3: asyncio.Queue = asyncio.Queue(maxsize=64)
        slot = LatestSlot()
        tail = TailIngest(
            self._source,
            start_after,
            first_boundary=first_boundary,
            poll_s=self._poll_s,
            grace_s=self._grace_s,
            on_scan=state.window.append,
            on_advance=advance,
        )

        self._q1 = q1
        self._tail_task = asyncio.create_task(tail.run(q1), name="tail-ingest")
        self._tasks = [
            self._tail_task,
            asyncio.create_task(WindowFilter(start_after).run(q1, q2), name="window"),
            asyncio.create_task(AggregationFilter().run(q2, q3), name="aggregation"),
            asyncio.create_task(RankingFilter().run(q3, slot), name="ranking"),
            asyncio.create_task(OutputFilter(on_publish=publish).run(slot), name="output"),
        ]
        try:
            # The tail only ends by cancellation (stop()); the downstream filters
            # end when TERMINATE reaches them. Any exception fails the whole run.
            await self._wait_downstream()
        finally:
            await self._cancel_filters()

    async def _wait_downstream(self) -> None:
        downstream = self._tasks[1:]
        pending = set(self._tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                if (exc := task.exception()) is not None:
                    raise exc
            if all(t.done() for t in downstream):
                return
            if self._tail_task in done and not self._tail_task.cancelled():
                raise RuntimeError("tail ingest exited unexpectedly")

    async def _cancel_filters(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def stop(self) -> None:
        """Graceful shutdown: stop the tail, let TERMINATE drain downstream."""
        if self._tail_task is None or self._q1 is None:
            return
        self._tail_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._tail_task
        await self._q1.put(TERMINATE)
        downstream = self._tasks[1:]
        if downstream:
            await asyncio.wait(downstream, timeout=self._drain_s)
