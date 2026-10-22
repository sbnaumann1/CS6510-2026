"""Scripted stand-ins for Analytics' scan source, so pipeline tests need no HTTP."""

from __future__ import annotations

import asyncio
from typing import Sequence

from analytics_service.scan_source import ScanRow
from checkout_web.errors import UpstreamUnavailable


class FakeScanSource:
    """An in-memory `ScanSource` over a mutable set of committed (id -> sku).

    `commit(*ids)` makes scans visible later (a late-committing id); an id that is never
    committed is a hole. `fail_next(n)` makes the next n reads raise UpstreamUnavailable,
    `delay` slows every read, and `log` records each read's size.
    """

    def __init__(self, ids=(), log: list | None = None, delay: float = 0.0):
        self.rows: dict[int, str] = {i: f"SKU-{i:06d}" for i in ids}
        self.log = log
        self.delay = delay
        self.max_override: int | None = None
        self._failures = 0

    def commit(self, *ids: int) -> None:
        self.rows.update({i: f"SKU-{i:06d}" for i in ids})

    def fail_next(self, n: int) -> None:
        self._failures = n

    @property
    def max_id(self) -> int:
        if self.max_override is not None:
            return self.max_override
        return max(self.rows, default=0)

    async def read(self, after: int, limit: int) -> tuple[Sequence[ScanRow], int]:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._failures > 0:
            self._failures -= 1
            raise UpstreamUnavailable("checkout", "scripted failure")
        got = [ScanRow(i, self.rows[i]) for i in sorted(self.rows) if i > after][:limit]
        if self.log is not None:
            self.log.append(("read", len(got)))
        return got, self.max_id

    async def max_scan_id(self) -> int:
        _, max_id = await self.read(0, 0)
        return max_id
