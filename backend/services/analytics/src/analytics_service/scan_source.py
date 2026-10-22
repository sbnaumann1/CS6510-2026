"""Where Analytics reads scans from: Checkout's `GET /internal/scans`.

`ScanSource` is the seam the pipeline depends on, so the filters and the gap logic can be
tested against a scripted in-memory source (tests/unit/_fakes.py) with no HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

import httpx

from analytics_service.config import settings
from checkout_web.client import ServiceClient
from checkout_web.errors import UpstreamUnavailable

READ_TIMEOUT_S = 2.0

_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class ScanRow:
    scan_id: int
    sku: str


class ScanSource(Protocol):
    async def read(self, after: int, limit: int) -> tuple[Sequence[ScanRow], int]:
        """Scans with id > `after` in id order (at most `limit`), and the highest
        committed scan id overall. Raises UpstreamUnavailable if unreachable."""

    async def max_scan_id(self) -> int: ...


class HttpScanSource:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = ServiceClient(base_url, transport=transport, peer="checkout")

    async def read(self, after: int, limit: int) -> tuple[Sequence[ScanRow], int]:
        response = await self._client.request(
            "GET",
            "/internal/scans",
            params={"after": after, "limit": limit},
            timeout=READ_TIMEOUT_S,
            retries=0,
            idempotent=True,
        )
        if response.status_code != 200:
            raise UpstreamUnavailable("checkout", f"HTTP {response.status_code}")
        body = response.json()
        return [ScanRow(s["scanId"], s["sku"]) for s in body["scans"]], int(body["maxScanId"])

    async def max_scan_id(self) -> int:
        _, max_id = await self.read(0, 0)
        return max_id

    async def aclose(self) -> None:
        await self._client.aclose()


def default_source() -> HttpScanSource:
    return HttpScanSource(settings.checkout_url, _transport)


def reset(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Tests pass an in-process transport."""
    global _transport
    _transport = transport
