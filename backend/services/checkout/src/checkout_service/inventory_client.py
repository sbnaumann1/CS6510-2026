"""Checkout's calls to Inventory (contracts/internal-apis.md): reserve and release.

Both are idempotent (keyed by the transaction id), so both retry within a time budget.
Anything that is not a clear answer raises `UpstreamUnavailable`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import httpx

from checkout_service.config import settings
from checkout_web.client import ServiceClient
from checkout_web.errors import UpstreamUnavailable

RESERVE_TIMEOUT_S = 2.0
RETRIES = 2

_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class Applied:
    replayed: bool


@dataclass(frozen=True)
class Insufficient:
    short_skus: list[str]


@dataclass(frozen=True)
class ReservationReleased:
    """Inventory already recorded a release for this transaction."""


class InventoryClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = ServiceClient(base_url, transport=transport, peer="inventory")

    async def reserve(
        self, public_id: str, lines: Sequence[tuple[str, int]]
    ) -> Applied | Insufficient | ReservationReleased:
        response = await self._client.request(
            "POST",
            "/internal/reservations",
            json={
                "transactionId": public_id,
                "lines": [{"sku": sku, "quantity": qty} for sku, qty in lines],
            },
            timeout=RESERVE_TIMEOUT_S,
            retries=RETRIES,
            idempotent=True,
            budget_s=settings.reserve_budget_s,
        )
        body = response.json() if response.content else {}
        if response.status_code == 200:
            return Applied(replayed=bool(body.get("replayed")))
        if response.status_code == 409 and body.get("error") == "INSUFFICIENT_STOCK":
            return Insufficient(list(body.get("shortSkus", [])))
        if response.status_code == 409 and body.get("error") == "RESERVATION_RELEASED":
            return ReservationReleased()
        raise UpstreamUnavailable("inventory", f"HTTP {response.status_code}")

    async def release(self, public_id: str) -> None:
        response = await self._client.request(
            "DELETE",
            f"/internal/reservations/{public_id}",
            timeout=RESERVE_TIMEOUT_S,
            retries=RETRIES,
            idempotent=True,
            budget_s=settings.reserve_budget_s,
        )
        if response.status_code != 200:
            raise UpstreamUnavailable("inventory", f"HTTP {response.status_code}")

    async def aclose(self) -> None:
        await self._client.aclose()


_client: InventoryClient | None = None


def get_client() -> InventoryClient:
    global _client
    if _client is None:
        _client = InventoryClient(settings.inventory_url, _transport)
    return _client


def reset(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Drop the client; tests pass an in-process transport."""
    global _client, _transport
    _client = None
    _transport = transport
