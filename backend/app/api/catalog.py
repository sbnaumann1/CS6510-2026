"""GET /items — the full catalog (FR-010)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app import catalog_cache

router = APIRouter(tags=["catalog"])


@router.get("/items")
async def list_items() -> Response:
    # Pre-rendered at startup in ascending SKU order (research R7, R12). Served as
    # raw bytes: no per-request serialization, no response_model revalidation.
    return Response(content=catalog_cache.items_body(), media_type="application/json")
