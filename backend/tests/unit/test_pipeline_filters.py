"""Each persistent pipeline filter, driven through real queues with no database.

Carries 004 SC3 forward: every filter is testable in isolation. Design:
specs/005-service-architecture/research.md R5, data-model.md §3.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter

import pytest

from analytics_service.pipeline import (
    TERMINATE,
    AggregationFilter,
    LatestSlot,
    OutputFilter,
    RankedData,
    RankingFilter,
    ScanData,
    Slide,
    Watermark,
    WindowCounts,
    WindowFilter,
)

WINDOW, SLIDE = 1000, 500


def sku_of(scan_id: int) -> str:
    return f"SKU-{scan_id % 7:06d}"


async def drain(q: asyncio.Queue, n: int | None = None, timeout: float = 2.0) -> list:
    """Read items until TERMINATE (exclusive) or n items."""
    out = []
    while n is None or len(out) < n:
        item = await asyncio.wait_for(q.get(), timeout)
        if item is TERMINATE:
            break
        out.append(item)
    return out


# --- WindowFilter -----------------------------------------------------------


async def run_window(items: list, start_after: int = 0) -> list[Slide]:
    inq, outq = asyncio.Queue(), asyncio.Queue()
    for item in items:
        inq.put_nowait(item)
    inq.put_nowait(TERMINATE)
    await WindowFilter(start_after=start_after, window=WINDOW, slide=SLIDE).run(inq, outq)
    return await drain(outq)


async def test_window_slides_only_on_watermarks_and_by_scan_id():
    items: list = [ScanData(i, sku_of(i)) for i in range(1, 1601)]
    # Watermarks right after their boundary scan, as TailIngest emits them.
    for b in (1500, 1000, 500):
        items.insert(b, Watermark(b))

    slides = await run_window(items)

    assert [(s.window_start, s.window_end) for s in slides] == [(0, 500), (0, 1000), (500, 1500)]
    for s in slides:
        b = s.window_end
        assert Counter(s.added) == Counter(sku_of(i) for i in range(b - SLIDE + 1, b + 1))
        assert Counter(s.evicted) == Counter(
            sku_of(i) for i in range(max(1, b - SLIDE - WINDOW + 1), b - WINDOW + 1)
        )


async def test_window_emits_nothing_without_a_watermark():
    assert await run_window([ScanData(i, "A") for i in range(1, 1201)]) == []


async def test_window_bootstrap_counts_the_whole_first_window():
    # Restart case: tail starts at b0-1000 and the first watermark is b0=2000.
    items: list = [ScanData(i, sku_of(i)) for i in range(1001, 2001)] + [Watermark(2000)]
    (slide,) = await run_window(items, start_after=1000)
    assert (slide.window_start, slide.window_end) == (1000, 2000)
    assert len(slide.added) == 1000 and slide.evicted == []


# --- AggregationFilter ------------------------------------------------------


async def test_aggregation_counts_equal_a_fresh_count_after_any_slides():
    rng = random.Random(5)
    scans = {i: rng.choice("ABCDEFG") for i in range(1, 3501)}
    inq, outq = asyncio.Queue(), asyncio.Queue()
    for b in range(SLIDE, 3501, SLIDE):
        added = [scans[i] for i in range(b - SLIDE + 1, b + 1)]
        evicted = [scans[i] for i in range(max(1, b - SLIDE - WINDOW + 1), b - WINDOW + 1)]
        inq.put_nowait(Slide(max(0, b - WINDOW), b, added, evicted))
    inq.put_nowait(TERMINATE)

    await AggregationFilter().run(inq, outq)
    windows: list[WindowCounts] = await drain(outq)

    assert [w.window_end for w in windows] == list(range(SLIDE, 3501, SLIDE))
    for w in windows:
        expected = Counter(scans[i] for i in range(w.window_start + 1, w.window_end + 1))
        assert w.counts == dict(expected)
        assert all(c > 0 for c in w.counts.values())


# --- RankingFilter ----------------------------------------------------------


async def test_ranking_breaks_ties_by_sku_and_truncates():
    inq, outq = asyncio.Queue(), asyncio.Queue()
    inq.put_nowait(WindowCounts(0, 500, {"B": 2, "A": 2, "C": 3}))
    many = {f"S{i:04d}": 1 for i in range(300)}
    inq.put_nowait(WindowCounts(500, 1500, many))
    inq.put_nowait(TERMINATE)

    await RankingFilter(depth=200).run(inq, outq)
    first, second = await drain(outq)

    assert [r["sku"] for r in first.ranking] == ["C", "A", "B"]
    assert first.ranking[0] == {"sku": "C", "scanCount": 3}
    assert (first.window_start, first.window_end) == (0, 500)
    assert len(second.ranking) == 200
    assert second.ranking[0]["sku"] == "S0000"


# --- LatestSlot + OutputFilter ----------------------------------------------


def ranked(end: int) -> RankedData:
    return RankedData(WINDOW, SLIDE, max(0, end - WINDOW), end, None, [{"sku": "A", "scanCount": 1}])


async def test_latest_slot_keeps_only_the_newest_unconsumed_item():
    slot = LatestSlot()
    for end in (500, 1000, 1500):
        await slot.put(ranked(end))
    await slot.put(TERMINATE)
    assert (await slot.get()).window_end == 1500
    assert await slot.get() is TERMINATE


async def test_output_publishes_only_the_latest_of_a_burst():
    published: list = []
    slot = LatestSlot()
    for end in (500, 1000, 1500):
        await slot.put(ranked(end))
    await slot.put(TERMINATE)

    await OutputFilter(on_publish=published.append).run(slot)

    assert [(r.window_start, r.window_end) for r in published] == [(500, 1500)]
    assert published[0].computed_at is not None, "stamped when published"


async def test_output_publishes_each_window_and_logs_it(caplog):
    caplog.set_level(logging.INFO, logger="analytics")
    published: list = []
    slot = LatestSlot()
    task = asyncio.create_task(OutputFilter(on_publish=published.append).run(slot))
    for end in (500, 1000, 1500):
        await slot.put(ranked(end))
        for _ in range(100):  # let the consumer publish it before the next arrives
            if len(published) == end // SLIDE:
                break
            await asyncio.sleep(0.005)
    await slot.put(TERMINATE)
    await asyncio.wait_for(task, 2)

    assert [r.window_end for r in published] == [500, 1000, 1500]
    assert "window published start=500 end=1500" in caplog.text


async def test_output_never_publishes_an_older_window():
    published: list = []
    slot = LatestSlot()
    task = asyncio.create_task(OutputFilter(on_publish=published.append).run(slot))
    await slot.put(ranked(1000))
    await asyncio.sleep(0.05)
    await slot.put(ranked(500))
    await slot.put(TERMINATE)
    await asyncio.wait_for(task, 2)
    assert [(r.window_start, r.window_end) for r in published] == [(0, 1000)]


# --- shutdown ----------------------------------------------------------------


@pytest.mark.parametrize("make", [
    lambda: WindowFilter(start_after=0, window=WINDOW, slide=SLIDE),
    lambda: AggregationFilter(),
    lambda: RankingFilter(depth=200),
])
async def test_terminate_stops_each_filter_and_is_forwarded(make):
    inq, outq = asyncio.Queue(), asyncio.Queue()
    inq.put_nowait(TERMINATE)
    await asyncio.wait_for(make().run(inq, outq), 1)
    assert outq.get_nowait() is TERMINATE
