"""Owns the one persistent pipeline: leadership, bootstrap, restart, shutdown.

Lifecycle (specs/005-service-architecture/data-model.md §3):
  standby       — retry the leader lock every 2 s until this process holds it
  bootstrapping — tail from b0 - window so the first window published is complete
  streaming     — five filters run until shutdown; windows are published as they slide
  restarting    — any filter error: cancel all filters, back off 1 s, bootstrap again

The lock is a session-level advisory lock on a dedicated connection held for the
leader's whole lifetime, so at most one pipeline runs across all Analytics
replicas (spec FR-015); PostgreSQL releases it if the leader dies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Callable

from analytics_service.pipeline import (
    SLIDE_INTERVAL,
    TERMINATE,
    WINDOW_SIZE,
    AggregationFilter,
    LatestSlot,
    OutputFilter,
    RankedData,
    RankingFilter,
    TailIngest,
    WindowFilter,
)
from checkout_data import (
    POPULAR_RECOMPUTE_LOCK,
    SessionLocal,
    advisory_unlock,
    engine,
    try_advisory_lock,
)
from checkout_data.repos import analytics as repo

log = logging.getLogger("analytics")


class PipelineSupervisor:
    def __init__(
        self,
        on_publish: Callable[[RankedData], None] | None = None,
        *,
        retry_s: float = 2.0,
        backoff_s: float = 1.0,
        drain_s: float = 2.0,
    ):
        self._on_publish = on_publish
        self._retry_s = retry_s
        self._backoff_s = backoff_s
        self._drain_s = drain_s
        self.instances = 0  # pipelines built; read by tests (one per bootstrap)
        self._tail_task: asyncio.Task | None = None
        self._q1: asyncio.Queue | None = None
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        async with engine.connect() as conn:
            await self._acquire(conn)
            try:
                while True:
                    try:
                        await self._run_pipeline()
                        return  # drained after stop()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("pipeline restarting after error")
                        await asyncio.sleep(self._backoff_s)
            finally:
                await self._cancel_filters()
                with contextlib.suppress(Exception):
                    await advisory_unlock(conn, POPULAR_RECOMPUTE_LOCK)
                    await conn.commit()

    async def _acquire(self, conn) -> None:
        waiting = False
        while True:
            got = await try_advisory_lock(conn, POPULAR_RECOMPUTE_LOCK)
            # The lock is session-level; commit so the connection does not sit
            # idle in a transaction for the leader's whole lifetime.
            await conn.commit()
            if got:
                return
            if not waiting:
                log.info("pipeline standby: another Analytics process is leader")
                waiting = True
            await asyncio.sleep(self._retry_s)

    async def _run_pipeline(self) -> None:
        async with SessionLocal() as session:
            max_seq = await repo.max_scan_seq(session)
        b0 = max_seq - max_seq % SLIDE_INTERVAL
        start_after = max(0, b0 - WINDOW_SIZE)
        # The first window published is b0 itself (complete), never a half-filled
        # earlier boundary; below the first boundary the read path answers on demand.
        first_boundary = b0 if b0 > 0 else SLIDE_INTERVAL

        self.instances += 1
        log.info("pipeline started bootstrap_from=%d", start_after)

        q1: asyncio.Queue = asyncio.Queue(maxsize=10000)
        q2: asyncio.Queue = asyncio.Queue(maxsize=64)
        q3: asyncio.Queue = asyncio.Queue(maxsize=64)
        slot = LatestSlot()
        tail = TailIngest(SessionLocal, start_after, first_boundary=first_boundary)

        self._q1 = q1
        self._tail_task = asyncio.create_task(tail.run(q1), name="tail-ingest")
        self._tasks = [
            self._tail_task,
            asyncio.create_task(WindowFilter(start_after).run(q1, q2), name="window"),
            asyncio.create_task(AggregationFilter().run(q2, q3), name="aggregation"),
            asyncio.create_task(RankingFilter().run(q3, slot), name="ranking"),
            asyncio.create_task(
                OutputFilter(SessionLocal, on_publish=self._on_publish).run(slot), name="output"
            ),
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
