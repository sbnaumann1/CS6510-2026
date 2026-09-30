"""Pipeline-based popular items analytics.

Refactored windowed analytics using a multi-stage filter pipeline with asyncio queues
for inter-filter communication. Stages process scan data independently and asynchronously:

1. IngestFilter: Reads scan sequences from DB
2. AggregationFilter: Counts item scans within window bounds
3. RankingFilter: Ranks top items by scan count
4. OutputFilter: Persists snapshot to DB
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog_cache
from app.config import settings
from app.db import analytics_repo as repo

log = logging.getLogger("checkout")

RANKING_DEPTH = 200


@dataclass
class ScanData:
    """Scan sequence data from DB."""
    sku: str
    scan_id: int


@dataclass
class AggregatedData:
    """Aggregated scan counts."""
    sku: str
    scan_count: int


@dataclass
class RankedData:
    """Final ranked data ready for persistence."""
    window_size: int
    slide_interval: int
    window_start: int
    window_end: int
    ranking: list[dict[str, Any]]


class IngestFilter:
    """Reads scan data from DB and emits to downstream queue."""

    def __init__(self, session: AsyncSession, window_bounds: tuple[int, int]):
        self.session = session
        self.window_start, self.window_end = window_bounds

    async def run(self, output_queue: asyncio.Queue) -> None:
        """Fetch scans in window and emit to output queue."""
        try:
            rows = await repo.window_counts(
                self.session, self.window_start, self.window_end, RANKING_DEPTH
            )
            for row in rows:
                data = AggregatedData(sku=row.sku, scan_count=row.scan_count)
                await output_queue.put(data)
            await output_queue.put(None)  # Signal end of stream
        except Exception as e:
            log.exception("IngestFilter error: %s", e)
            await output_queue.put(None)


class AggregationFilter:
    """Buffers and aggregates scan counts from input queue."""

    async def run(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        """Consume aggregated data and forward to ranking."""
        try:
            while True:
                data = await input_queue.get()
                if data is None:
                    await output_queue.put(None)
                    break
                await output_queue.put(data)
        except Exception as e:
            log.exception("AggregationFilter error: %s", e)
            await output_queue.put(None)


class RankingFilter:
    """Ranks items and prepares final output."""

    def __init__(self, window_bounds: tuple[int, int]):
        self.window_start, self.window_end = window_bounds

    async def run(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        """Consume aggregated data, rank items, and emit RankedData."""
        try:
            ranking = []
            while True:
                data = await input_queue.get()
                if data is None:
                    ranked = RankedData(
                        window_size=settings.popular_window_size,
                        slide_interval=settings.popular_slide_interval,
                        window_start=self.window_start,
                        window_end=self.window_end,
                        ranking=ranking,
                    )
                    await output_queue.put(ranked)
                    await output_queue.put(None)
                    break
                ranking.append({"sku": data.sku, "scanCount": data.scan_count})
        except Exception as e:
            log.exception("RankingFilter error: %s", e)
            await output_queue.put(None)


class OutputFilter:
    """Writes final results to database."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def run(self, input_queue: asyncio.Queue) -> None:
        """Consume RankedData and persist to DB."""
        try:
            while True:
                data = await input_queue.get()
                if data is None:
                    break
                ranking_json = orjson.dumps(data.ranking).decode()
                await repo.upsert_snapshot(
                    self.session,
                    data.window_size,
                    data.slide_interval,
                    data.window_start,
                    data.window_end,
                    ranking_json,
                )
            await self.session.commit()
        except Exception as e:
            log.exception("OutputFilter error: %s", e)


async def recompute_windowed(session: AsyncSession) -> None:
    """Recompute window using pipeline architecture with queues for inter-filter communication."""
    max_seq = await repo.max_scan_seq(session)
    window_start = max(0, max_seq - settings.popular_window_size)
    window_bounds = (window_start, max_seq)

    ingest_queue = asyncio.Queue()
    aggregation_queue = asyncio.Queue()
    output_queue = asyncio.Queue()

    ingest = IngestFilter(session, window_bounds)
    aggregation = AggregationFilter()
    ranking = RankingFilter(window_bounds)
    output = OutputFilter(session)

    await asyncio.gather(
        ingest.run(ingest_queue),
        aggregation.run(ingest_queue, aggregation_queue),
        ranking.run(aggregation_queue, output_queue),
        output.run(output_queue),
    )
