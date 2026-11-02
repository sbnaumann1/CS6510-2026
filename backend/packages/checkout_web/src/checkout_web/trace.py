"""Wall-clock trace points for profiling the completion path (spec 009 research).

Off unless TRACE_TIMING=1, so the default build pays one boolean test per call. Each point is
one `step: "trace"` log line whose `outcome` is the point name and whose `ts` is epoch
milliseconds; every container shares the host clock, so points from different services can be
subtracted. scripts/profile_completion.py joins them by `tx` and reports each segment.
"""

from __future__ import annotations

import os
import time

from checkout_web.request import log_step

ENABLED = os.getenv("TRACE_TIMING") == "1"


def trace(point: str, tx: str | None = None, **extra) -> None:
    if ENABLED:
        log_step("trace", outcome=point, tx=tx, ts=round(time.time() * 1000, 2), pid=os.getpid(), **extra)
