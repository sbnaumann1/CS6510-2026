"""Where Analytics reads scans from: Checkout's scan events on `checkout.scans.v1`
(spec 008 research R8; replaces 007's HTTP pull of `GET /internal/scans`).

`ScanSource` is the seam the pipeline depends on, so the filters and the gap logic are
unchanged and can still be tested against a scripted in-memory source (tests/unit/_fakes.py).

`KafkaScanSource` consumes the topic in order into a bounded in-memory buffer keyed by scan
id and answers `read(after, limit)` from it. On start it replays only the last
`2 x window size` events, which is enough to rebuild the current window without any call to
Checkout (the window is the last 1 000 scans; the pipeline bootstraps from at most
`window + slide` ids below the newest one). A scan id it has already seen is ignored, so the
relay's at-least-once re-publishing is harmless. A `scan-log-reset` event clears the buffer
and changes `log_id`, which the pipeline treats as "Checkout's data was reset".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Sequence

from analytics_service import eventbus
from analytics_service.config import settings
from checkout_web.bus import Bus, Record
from checkout_web.errors import UpstreamUnavailable
from checkout_web.events import CHECKOUT_SCANS, SCAN_LOG_RESET, SCAN_RECORDED, EventDecodeError

log = logging.getLogger("analytics")


@dataclass(frozen=True)
class ScanRow:
    scan_id: int
    sku: str


class ScanSource(Protocol):
    async def read(self, after: int, limit: int) -> tuple[Sequence[ScanRow], int]:
        """Scans with id > `after` in id order (at most `limit`), and the highest
        scan id seen overall. Raises UpstreamUnavailable if the source is not ready."""

    async def max_scan_id(self) -> int: ...

    # Optional: `log_id`, an identifier of the log being read. A source that has one lets the
    # pipeline notice that the log was replaced (checked with getattr, so fakes need none).


class KafkaScanSource:
    def __init__(self, bus: Bus, *, replay: int | None = None, keep: int | None = None) -> None:
        window = settings.popular_window_size
        self._bus = bus
        self._replay = replay if replay is not None else 2 * window
        self._keep = keep if keep is not None else 4 * window
        self._rows: dict[int, str] = {}
        self._max_id = 0
        self._caught_up = False
        self._consumer = None
        self.log_id: str | None = None

    @property
    def caught_up(self) -> bool:
        """The replay of the retained tail has been read: the window can be rebuilt."""
        return self._caught_up

    @property
    def highest_applied(self) -> int:
        return self._max_id

    async def run(self) -> None:
        """Read the topic forever."""
        self._consumer = self._bus.consumer(CHECKOUT_SCANS, start=self._replay)
        await self._consumer.start()
        while True:
            records = await self._consumer.poll(settings.scan_poll_ms)
            if not records:
                # Nothing more to read right now: the replay is done (or the topic is empty).
                self._caught_up = True
                continue
            for rec in records:
                self._apply(rec)

    def _apply(self, rec: Record) -> None:
        try:
            event = rec.event()
        except EventDecodeError:
            return  # a malformed record cannot be a scan we could count
        if event.type == SCAN_RECORDED:
            scan_id = int(event.data["scanId"])
            if scan_id in self._rows:
                return  # a duplicate from the relay's at-least-once publishing
            self._rows[scan_id] = event.data["sku"]
            self._max_id = max(self._max_id, scan_id)
            while len(self._rows) > self._keep:
                del self._rows[next(iter(self._rows))]
        elif event.type == SCAN_LOG_RESET:
            self._rows.clear()
            self._max_id = 0
            self.log_id = event.data.get("logId")
            log.warning("checkout's scan log was reset (log %s)", self.log_id)

    async def read(self, after: int, limit: int) -> tuple[Sequence[ScanRow], int]:
        if not self._caught_up:
            raise UpstreamUnavailable("checkout", "scan events not read yet")
        ids = sorted(i for i in self._rows if i > after)[:limit]
        return [ScanRow(i, self._rows[i]) for i in ids], self._max_id

    async def max_scan_id(self) -> int:
        _, max_id = await self.read(0, 0)
        return max_id


_source: KafkaScanSource | None = None


def default_source() -> KafkaScanSource:
    global _source
    if _source is None:
        _source = KafkaScanSource(eventbus.get_bus())
    return _source


def reset() -> None:
    """Forget the source; the next start builds a fresh one."""
    global _source
    _source = None
