"""Transaction endpoints.

Request validation is done by hand rather than through response_model/Pydantic on
the hot paths (research R7), and error precedence follows error-catalog.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import background
from app.db import get_session
from app.errors import INVALID_REQUEST, ApiError
from app.services import transactions as svc

router = APIRouter(tags=["transactions"])


async def _json_body(request: Request, *, required: bool) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        if required:
            raise ApiError(INVALID_REQUEST, "A JSON body is required.")
        return {}
    try:
        import orjson

        body = orjson.loads(raw)
    except Exception:
        raise ApiError(INVALID_REQUEST, "Request body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise ApiError(INVALID_REQUEST, "Request body must be a JSON object.")
    return body


def _required_str(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ApiError(INVALID_REQUEST, f"'{field}' must be a non-empty string.")
    return value


@router.post("/transactions", status_code=201)
async def start_transaction(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    body = await _json_body(request, required=True)
    station_id = _required_str(body, "stationId")
    result = await svc.start_transaction(session, station_id)
    return ORJSONResponse(status_code=201, content=result)


@router.post("/transactions/{transaction_id}/items")
async def scan_item(
    transaction_id: str,
    request: Request,
    tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> Response:
    body = await _json_body(request, required=True)
    sku = _required_str(body, "sku")
    # Malformed IDs are rejected before any query (research R5).
    tx_id = svc.parse_tx_id(transaction_id)

    result, scan_seq = await svc.scan_item(session, tx_id, sku)
    if background.should_recompute(scan_seq):
        tasks.add_task(background.recompute_window)
    return ORJSONResponse(content=result)


@router.post("/transactions/{transaction_id}/complete")
async def complete_transaction(
    transaction_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    # The load client sends `{}`; a missing body is also accepted.
    await _json_body(request, required=False)
    tx_id = svc.parse_tx_id(transaction_id)
    result = await svc.complete_transaction(session, tx_id)
    return ORJSONResponse(content=result)


@router.get("/transactions/{transaction_id}")
async def get_transaction(
    transaction_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    tx_id = svc.parse_tx_id(transaction_id)
    result = await svc.get_transaction(session, tx_id)
    return ORJSONResponse(content=result)
