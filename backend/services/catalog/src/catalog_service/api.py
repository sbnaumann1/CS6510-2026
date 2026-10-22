"""GET /items — the full catalog (FR-010)."""

from __future__ import annotations

import orjson
from fastapi import APIRouter, Response

from checkout_web.money import cents_to_amount

router = APIRouter(tags=["catalog"])

_body: bytes = b'{"items":[]}'


def render(rows: list[tuple[str, str, int]]) -> bytes:
    """Pre-render the response body once, in the given (ascending SKU) order."""
    return orjson.dumps(
        {
            "items": [
                {"sku": sku, "name": name, "price": cents_to_amount(cents)}
                for sku, name, cents in rows
            ]
        }
    )


def set_body(body: bytes) -> None:
    global _body
    _body = body


@router.get("/items")
async def list_items() -> Response:
    # Pre-rendered at startup in ascending SKU order (001 research R7, R12).
    # Served as raw bytes: no per-request serialization.
    return Response(content=_body, media_type="application/json")
