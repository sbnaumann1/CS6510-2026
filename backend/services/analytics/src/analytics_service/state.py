"""Analytics' whole state, in memory. Nothing here is persisted.

Everything is derived from Checkout's scan log and rebuilt from it on start. One
instance runs, so a module-level singleton is the right shape.
"""

from __future__ import annotations

from analytics_service.config import settings
from analytics_service.scan_window import ScanWindow

# Rolling window of recent scans; fed by the tail as scans are accepted in id order.
window = ScanWindow(settings.popular_window_size, settings.popular_slide_interval)

# The latest published ranking (a `pipeline.RankedData`), or None before the first slide.
latest = None

# The tail's contiguous watermark: every scan id up to it has been read or skipped as a
# hole that never committed. `GET /internal/reports/cursor` reports it.
cursor = 0


def reset() -> None:
    """Forget everything, as a fresh process would (also used when Checkout's data is reset)."""
    global latest, cursor
    window.clear()
    latest = None
    cursor = 0
