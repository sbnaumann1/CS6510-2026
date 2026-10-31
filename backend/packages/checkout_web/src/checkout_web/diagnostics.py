"""Saturation probes for profiling (spec 009 research); inert unless TRACE_TIMING=1.

Two questions a latency trace cannot answer by itself: is this worker's event loop too busy to
run its coroutines on time, and are requests queueing for a database connection? Both look like
"every operation got slower".

* `pool_kwargs()` swaps in a connection pool that times how long each checkout waited.
* `monitor()` is a task that sleeps 10 ms in a loop and measures how late it wakes (event-loop
  lag), samples how many pool connections are in use, and every 5 s logs one `step: "monitor"`
  line with the loop lag, the pool wait and the pool occupancy for that interval.

scripts/summarize_diagnostics.py reads those lines together with the `docker stats` samples.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from checkout_web.request import log_step
from checkout_web.trace import ENABLED

_waits_ms: list[float] = []


def pool_kwargs(base_pool: type) -> dict[str, Any]:
    """`poolclass=` for create_async_engine. The service passes SQLAlchemy's
    AsyncAdaptedQueuePool, so this shared package imports no database library."""
    if not ENABLED:
        return {}

    class TimedPool(base_pool):
        def _do_get(self):
            t0 = time.perf_counter()
            try:
                return super()._do_get()
            finally:
                _waits_ms.append((time.perf_counter() - t0) * 1000)

    return {"poolclass": TimedPool}


def _pct(v: list[float], p: float) -> float:
    return round(v[min(len(v) - 1, int(len(v) * p))], 2) if v else 0.0


async def monitor(engine, interval_s: float = 5.0, tick_s: float = 0.01) -> None:
    if not ENABLED:
        return
    pool = engine.sync_engine.pool
    lags: list[float] = []
    used: list[int] = []
    loop = asyncio.get_running_loop()
    next_report = loop.time() + interval_s
    while True:
        t0 = loop.time()
        await asyncio.sleep(tick_s)
        lags.append(max(0.0, (loop.time() - t0 - tick_s) * 1000))
        used.append(pool.checkedout())
        if loop.time() >= next_report:
            next_report = loop.time() + interval_s
            lags.sort()
            waits = sorted(_waits_ms)
            size = pool.size()
            log_step(
                "monitor", outcome="interval", pid=os.getpid(),
                lag_mean_ms=round(sum(lags) / len(lags), 2), lag_p95_ms=_pct(lags, .95),
                lag_p99_ms=_pct(lags, .99), lag_max_ms=round(lags[-1], 2),
                pool_size=size, pool_used_mean=round(sum(used) / len(used), 2), pool_used_max=max(used),
                pool_full_frac=round(sum(1 for u in used if u >= size) / len(used), 3),
                pool_gets=len(waits), pool_wait_mean_ms=round(sum(waits) / len(waits), 2) if waits else 0.0,
                pool_wait_p95_ms=_pct(waits, .95), pool_wait_p99_ms=_pct(waits, .99),
            )
            lags.clear(); used.clear(); _waits_ms.clear()
