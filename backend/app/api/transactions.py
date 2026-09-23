"""Transaction endpoints.

Request validation is done by hand rather than through response_model/Pydantic on
the hot paths (research R7), and error precedence follows error-catalog.md.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import background
from app.api._request import _json_body, _required_str
from app.db import get_session
from app.transactions import service as svc

router = APIRouter(tags=["transactions"])


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
