"""The rolling window of recent scans, held in memory (data-model.md §4).

It answers the read path before a full window has been published, which 005 answered
with a SQL count (`windowStart` stays 0 until a full window exists). It is rebuilt from
Checkout's scan log whenever the pipeline bootstraps.
"""

from __future__ import annotations

from collections import Counter, deque


class ScanWindow:
    def __init__(self, window_size: int, slide_interval: int):
        self.window_size = window_size
        # Room for the window plus one slide of scans that arrived ahead of the boundary.
        self._scans: deque[tuple[int, str]] = deque(maxlen=window_size + slide_interval)
        self.last_scan_id = 0

    def append(self, scan_id: int, sku: str) -> None:
        self._scans.append((scan_id, sku))
        if scan_id > self.last_scan_id:
            self.last_scan_id = scan_id

    def clear(self) -> None:
        self._scans.clear()
        self.last_scan_id = 0

    def bounds(self) -> tuple[int, int]:
        """(window_start exclusive, window_end inclusive) for the scans seen so far."""
        end = self.last_scan_id
        return max(0, end - self.window_size), end

    def counts(self, depth: int) -> list[tuple[str, int]]:
        """(sku, scan_count) for scans in the current bounds, by count descending then SKU."""
        start, end = self.bounds()
        counter = Counter(sku for scan_id, sku in self._scans if start < scan_id <= end)
        return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:depth]

    def __len__(self) -> int:
        return len(self._scans)
