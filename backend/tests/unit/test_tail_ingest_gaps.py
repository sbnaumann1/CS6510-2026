"""TailIngest's gap rule (specs/005-service-architecture/research.md R6).

Concurrent scans commit out of id order. The tail must never permanently skip a
late-committing id, must not stall on an id that never commits, and must emit a
slide watermark only once every id at or below it is accounted for.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

from analytics_service.pipeline import ScanData, TailIngest, Watermark


class CommittedScans:
    """Fake repo: get_scans_after over a mutable set of committed (id -> sku)."""

    def __init__(self, ids=(), log=None):
        self.rows: dict[int, str] = {i: f"SKU-{i:06d}" for i in ids}
        self.log = log

    def commit(self, *ids):
        self.rows.update({i: f"SKU-{i:06d}" for i in ids})

    async def get_scans_after(self, session, last_id, limit=1000):
        got = [SimpleNamespace(id=i, sku=self.rows[i]) for i in sorted(self.rows) if i > last_id][:limit]
        if self.log is not None:
            self.log.append(("read", len(got)))
        return got


@asynccontextmanager
async def no_session():
    yield None


def tail(repo, start_after=0, *, grace_s=10.0, sleep=None, **kw) -> TailIngest:
    return TailIngest(
        no_session, start_after, repo=repo, poll_s=0.002, grace_s=grace_s,
        **({"sleep": sleep} if sleep else {}), **kw,
    )


async def collect(q: asyncio.Queue, n: int, timeout=2.0) -> list:
    return [await asyncio.wait_for(q.get(), timeout) for _ in range(n)]


def scan_ids(items) -> list[int]:
    return [i.scan_id for i in items if isinstance(i, ScanData)]


async def test_late_commit_inside_grace_is_never_skipped():
    repo = CommittedScans([1, 2, 4, 5])
    out: asyncio.Queue = asyncio.Queue()
    t = tail(repo)
    task = asyncio.create_task(t.run(out))
    try:
        assert scan_ids(await collect(out, 2)) == [1, 2]
        await asyncio.sleep(0.05)
        assert out.empty(), "4 and 5 must wait for 3"
        repo.commit(3)
        assert scan_ids(await collect(out, 3)) == [3, 4, 5]
        assert t.last_id == 5
    finally:
        task.cancel()


async def test_permanent_gap_is_skipped_after_grace(caplog):
    caplog.set_level(logging.INFO, logger="analytics")
    repo = CommittedScans([1, 2, 4])
    out: asyncio.Queue = asyncio.Queue()
    t = tail(repo, grace_s=0.05)
    task = asyncio.create_task(t.run(out))
    try:
        assert scan_ids(await collect(out, 3)) == [1, 2, 4]
        assert t.last_id == 4
        assert "gap skipped id=3" in caplog.text
    finally:
        task.cancel()


async def test_consecutive_missing_ids_cost_one_grace_period():
    repo = CommittedScans([1, 5])
    out: asyncio.Queue = asyncio.Queue()
    t = tail(repo, grace_s=0.05)
    task = asyncio.create_task(t.run(out))
    try:
        loop = asyncio.get_running_loop()
        await collect(out, 1)
        started = loop.time()
        assert scan_ids(await collect(out, 1)) == [5]
        assert loop.time() - started < 0.5  # one grace period, not three
    finally:
        task.cancel()


async def test_watermark_follows_its_boundary_scan_exactly_once():
    repo = CommittedScans(range(1, 1001))
    out: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(tail(repo).run(out))
    try:
        items = await collect(out, 1002)
        marks = [(n, i.boundary) for n, i in enumerate(items) if isinstance(i, Watermark)]
        assert marks == [(500, 500), (1001, 1000)]  # right after ScanData 500 and 1000
        assert scan_ids(items) == list(range(1, 1001))
    finally:
        task.cancel()


async def test_first_watermark_after_bootstrap():
    repo = CommittedScans(range(1001, 1601))
    out: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(tail(repo, start_after=1000).run(out))
    try:
        items = await collect(out, 601)
        assert [i.boundary for i in items if isinstance(i, Watermark)] == [1500]
    finally:
        task.cancel()


async def test_explicit_first_boundary_suppresses_partial_windows():
    # Restart at b0=2000: tail from 1000, but 1500 would be a half-empty window.
    repo = CommittedScans(range(1001, 2001))
    out: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(tail(repo, start_after=1000, first_boundary=2000).run(out))
    try:
        items = await collect(out, 1001)
        assert [i.boundary for i in items if isinstance(i, Watermark)] == [2000]
    finally:
        task.cancel()


async def test_backlog_drains_without_sleeping_between_full_batches():
    log: list = []
    repo = CommittedScans(range(1, 3001), log=log)

    async def recording_sleep(s):
        log.append(("sleep",))
        await asyncio.sleep(0)

    out: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(tail(repo, sleep=recording_sleep).run(out))
    try:
        await collect(out, 3006)  # 3000 scans + 6 watermarks
        await asyncio.sleep(0.01)
        first_sleep = log.index(("sleep",))
        # Three full batches back to back, then the short read that shows it
        # has caught up — only then does it sleep.
        assert log[:first_sleep] == [("read", 1000)] * 3 + [("read", 0)]
    finally:
        task.cancel()


async def test_tail_stuck_behind_a_hole_sleeps_instead_of_spinning():
    log: list = []
    repo = CommittedScans([1, *range(3, 1003)], log=log)  # 2 missing, 1000 rows above it

    async def recording_sleep(s):
        log.append(("sleep",))
        await asyncio.sleep(0)

    out: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(tail(repo, sleep=recording_sleep).run(out))
    try:
        await collect(out, 1)
        for _ in range(20):
            await asyncio.sleep(0)
        reads = [e for e in log if e[0] == "read"]
        sleeps = [e for e in log if e[0] == "sleep"]
        assert len(sleeps) >= len(reads) - 1  # every no-progress read is followed by a sleep
    finally:
        task.cancel()
