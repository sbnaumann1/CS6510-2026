"""The event bus this Checkout process publishes to and reads from.

Set once by `main.start_background` (a KafkaBus in the service, a shared MemoryBus in the
in-process tests). A module-level holder rather than an import-time singleton, because all
four services run in one process under test and must not share one another's holder.
"""

from __future__ import annotations

from checkout_web.bus import Bus

_bus: Bus | None = None


def set_bus(bus: Bus | None) -> None:
    global _bus
    _bus = bus


def get_bus() -> Bus:
    if _bus is None:
        raise RuntimeError("the event bus has not been started (main.start_background)")
    return _bus
