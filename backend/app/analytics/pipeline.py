"""Streaming pipeline for popular items analytics.

Real-time hopping-window architecture where scan events flow continuously
through independent filters connected by asyncio queues:

1. IngestFilter: Polls database for new scans, emits ScanData
2. WindowFilter: Maintains 1000-scan sliding buffer, emits WindowData every 500 scans
3. AggregationFilter: Counts scans by SKU in current window
4. RankingFilter: Ranks items by scan count, emits RankedData
5. OutputFilter: Persists ranking to database non-blockingly
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from collections import Counter

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog_cache
from app.config import settings
from app.db import analytics_repo as repo

log = logging.getLogger("checkout")

# Configuration constants
WINDOW_SIZE = 1000
SLIDE_INTERVAL = 500
RANKING_DEPTH = 200

# Sentinel for pipeline shutdown
TERMINATE = object()


@dataclass(frozen=True)
class ScanData:
    """Represents a single scan event from transaction.

    Fields:
        sku: Item SKU (e.g., "SKU-000001")
        scan_id: Global scan sequence ID from transaction_item.id
    """
    sku: str
    scan_id: int


@dataclass(frozen=True)
class WindowData:
    """Represents current 1000-scan window with metadata.

    Fields:
        window_start: First scan_id in window (inclusive)
        window_end: Last scan_id in window (inclusive)
        scans: ScanData objects in current window
        slide_count: How many slides have occurred (for debugging)
    """
    window_start: int
    window_end: int
    scans: list[ScanData]
    slide_count: int = 0


@dataclass(frozen=True)
class AggregatedData:
    """Aggregated scan count for one SKU in current window.

    Fields:
        sku: Item SKU
        scan_count: Number of scans of this SKU in current window
    """
    sku: str
    scan_count: int


@dataclass(frozen=True)
class WindowMetadata:
    """Window metadata marker for downstream filters.

    Emitted by AggregationFilter to signal start of a new window's aggregates.
    """
    window_start: int
    window_end: int


@dataclass(frozen=True)
class RankedData:
    """Final ranked items ready for database persistence.

    Fields:
        window_size: Configured window size (1000)
        slide_interval: Configured slide interval (500)
        window_start: First scan_id in window
        window_end: Last scan_id in window
        computed_at: ISO timestamp of ranking computation
        ranking: Top 200 items, ordered by scan_count descending
    """
    window_size: int
    slide_interval: int
    window_start: int
    window_end: int
    computed_at: datetime
    ranking: list[dict[str, Any]]


class IngestFilter:
    """Polls database for new scans and emits ScanData events.

    Phase 1: Polling from database (simulates event stream)
    Future: Can be replaced with event hooks from transaction layer
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.last_scan_id = 0

    async def run(self, output_queue: asyncio.Queue) -> None:
        """Poll database for new scans and emit ScanData.

        Periodically queries transaction_item table for scans with id > last_scan_id.
        Emits each as ScanData. When no more scans are available, emits TERMINATE.
        """
        try:
            while True:
                rows = await repo.get_scans_after(self.session, self.last_scan_id, limit=100)
                if not rows:
                    await output_queue.put(TERMINATE)
                    break
                for row in rows:
                    data = ScanData(sku=row.sku, scan_id=row.id)
                    await output_queue.put(data)
                    self.last_scan_id = row.id
        except Exception as e:
            log.exception("IngestFilter error: %s", e)
            await output_queue.put(TERMINATE)


class WindowFilter:
    """Maintains sliding window of 1000 scans, emits WindowData every 500 scans.

    Uses a circular deque to buffer exactly WINDOW_SIZE scans. When SLIDE_INTERVAL
    new scans have been received, emits the complete window as WindowData.
    """

    def __init__(self):
        self.scans_buffer = deque(maxlen=WINDOW_SIZE)
        self.scan_count = 0
        self.slides_emitted = 0

    async def run(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        """Maintain sliding window and emit WindowData every SLIDE_INTERVAL scans."""
        try:
            while True:
                data = await input_queue.get()
                if data is TERMINATE:
                    # Emit final window if we have data
                    if self.scans_buffer:
                        window_data = WindowData(
                            window_start=self.scans_buffer[0].scan_id,
                            window_end=self.scans_buffer[-1].scan_id,
                            scans=list(self.scans_buffer),
                            slide_count=self.slides_emitted,
                        )
                        await output_queue.put(window_data)
                    await output_queue.put(TERMINATE)
                    break

                # Add scan to circular buffer
                self.scans_buffer.append(data)
                self.scan_count += 1

                # Emit window every SLIDE_INTERVAL scans
                if self.scan_count % SLIDE_INTERVAL == 0:
                    window_data = WindowData(
                        window_start=self.scans_buffer[0].scan_id,
                        window_end=self.scans_buffer[-1].scan_id,
                        scans=list(self.scans_buffer),
                        slide_count=self.slides_emitted,
                    )
                    await output_queue.put(window_data)
                    self.slides_emitted += 1
        except Exception as e:
            log.exception("WindowFilter error: %s", e)
            await output_queue.put(TERMINATE)


class AggregationFilter:
    """Counts scans by SKU for each window.

    Receives WindowData and emits WindowMetadata followed by AggregatedData
    for each unique SKU with its scan count in the current window.
    """

    async def run(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        """Aggregate scan counts by SKU and emit WindowMetadata + AggregatedData."""
        try:
            while True:
                data = await input_queue.get()
                if data is TERMINATE:
                    await output_queue.put(TERMINATE)
                    break

                # Emit window metadata first
                metadata = WindowMetadata(
                    window_start=data.window_start,
                    window_end=data.window_end,
                )
                await output_queue.put(metadata)

                # Count scans by SKU
                sku_counts = Counter(scan.sku for scan in data.scans)

                # Emit AggregatedData for each SKU
                for sku, count in sku_counts.items():
                    agg_data = AggregatedData(sku=sku, scan_count=count)
                    await output_queue.put(agg_data)
        except Exception as e:
            log.exception("AggregationFilter error: %s", e)
            await output_queue.put(TERMINATE)


class RankingFilter:
    """Ranks items by scan count for output.

    Collects AggregatedData for all SKUs in current window, ranks by scan_count
    descending, and emits RankedData with top RANKING_DEPTH items.
    """

    def __init__(self):
        self.window_metadata = None

    async def run(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        """Collect aggregated data and emit ranked output."""
        try:
            ranking_list = []
            window_start = None
            window_end = None

            while True:
                data = await input_queue.get()
                if data is TERMINATE:
                    # Sort by scan_count descending, take top RANKING_DEPTH
                    sorted_ranking = sorted(
                        ranking_list,
                        key=lambda x: x["scanCount"],
                        reverse=True
                    )[:RANKING_DEPTH]

                    if window_start is not None and window_end is not None:
                        ranked = RankedData(
                            window_size=WINDOW_SIZE,
                            slide_interval=SLIDE_INTERVAL,
                            window_start=window_start,
                            window_end=window_end,
                            computed_at=datetime.now(),
                            ranking=sorted_ranking,
                        )
                        await output_queue.put(ranked)

                    await output_queue.put(TERMINATE)
                    break

                # Handle WindowMetadata or AggregatedData
                if isinstance(data, WindowMetadata):
                    # New window; if we have data from previous, emit it
                    if ranking_list and window_start is not None:
                        sorted_ranking = sorted(
                            ranking_list,
                            key=lambda x: x["scanCount"],
                            reverse=True
                        )[:RANKING_DEPTH]
                        ranked = RankedData(
                            window_size=WINDOW_SIZE,
                            slide_interval=SLIDE_INTERVAL,
                            window_start=window_start,
                            window_end=window_end,
                            computed_at=datetime.now(),
                            ranking=sorted_ranking,
                        )
                        await output_queue.put(ranked)
                    window_start = data.window_start
                    window_end = data.window_end
                    ranking_list = []
                elif isinstance(data, AggregatedData):
                    ranking_list.append({"sku": data.sku, "scanCount": data.scan_count})
        except Exception as e:
            log.exception("RankingFilter error: %s", e)
            await output_queue.put(TERMINATE)


class OutputFilter:
    """Persists RankedData to database non-blockingly.

    Receives RankedData and writes to popular_window_snapshot table.
    Database commits are non-blocking to prevent pipeline delays.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def run(self, input_queue: asyncio.Queue) -> None:
        """Consume RankedData and persist to database."""
        try:
            while True:
                data = await input_queue.get()
                if data is TERMINATE:
                    break

                ranking_json = orjson.dumps(data.ranking).decode()
                await repo.upsert_snapshot(
                    self.session,
                    data.window_size,
                    data.slide_interval,
                    data.window_start,
                    data.window_end,
                    ranking_json,
                    data.computed_at,
                )

            # Non-blocking commit
            try:
                await self.session.commit()
            except Exception as e:
                log.exception("OutputFilter commit error: %s", e)
        except Exception as e:
            log.exception("OutputFilter error: %s", e)


async def run_streaming(session: AsyncSession) -> None:
    """Orchestrates streaming pipeline for popular items analytics.

    Coordinates five filters connected by asyncio queues to process scans
    in real-time. All filters run concurrently:

    - IngestFilter: Polls database for new scans
    - WindowFilter: Maintains 1000-scan sliding buffer
    - AggregationFilter: Counts scans by SKU
    - RankingFilter: Ranks items by scan count
    - OutputFilter: Persists to database

    Args:
        session: Async database session for queries and persistence
    """
    ingest_queue = asyncio.Queue()
    window_queue = asyncio.Queue()
    agg_queue = asyncio.Queue()
    rank_queue = asyncio.Queue()

    ingest = IngestFilter(session)
    window = WindowFilter()
    aggregation = AggregationFilter()
    ranking = RankingFilter()
    output = OutputFilter(session)

    await asyncio.gather(
        ingest.run(ingest_queue),
        window.run(ingest_queue, window_queue),
        aggregation.run(window_queue, agg_queue),
        ranking.run(agg_queue, rank_queue),
        output.run(rank_queue),
    )
