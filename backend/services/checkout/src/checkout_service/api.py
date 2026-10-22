"""Transaction endpoints.

Request validation is done by hand rather than through response_model/Pydantic on
the hot paths (001 research R7), and error precedence follows error-catalog.md.
Start and scan each run one atomic statement, so they use autocommit sessions and skip
the BEGIN/COMMIT round trips; get stays transactional (spec 006 R2). Complete is the
saga in saga.py and manages its own short transactions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import ORJSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from checkout_service import service as svc
from checkout_service.db import get_autocommit_session, get_session
from checkout_web.request import _json_body, _required_str

router = APIRouter(tags=["transactions"])


@router.post("/transactions", status_code=201)
async def start_transaction(
    request: Request, session: AsyncSession = Depends(get_autocommit_session)
) -> Response:
    body = await _json_body(request, required=True)
    station_id = _required_str(body, "stationId")
    result = await svc.start_transaction(session, station_id)
    return ORJSONResponse(status_code=201, content=result)


@router.post("/transactions/{transaction_id}/items")
async def scan_item(
    transaction_id: str,
    request: Request,
    session: AsyncSession = Depends(get_autocommit_session),
) -> Response:
    body = await _json_body(request, required=True)
    sku = _required_str(body, "sku")
    # Malformed IDs are rejected before any query (001 research R5).
    tx_id = svc.parse_tx_id(transaction_id)
    return ORJSONResponse(content=await svc.scan_item(session, tx_id, sku))


@router.post("/transactions/{transaction_id}/complete")
async def complete_transaction(transaction_id: str, request: Request) -> Response:
    # The load client sends `{}`; a missing body is also accepted.
    await _json_body(request, required=False)
    tx_id = svc.parse_tx_id(transaction_id)
    result = await svc.complete_transaction(tx_id)
    return ORJSONResponse(content=result)


@router.get("/transactions/{transaction_id}")
async def get_transaction(
    transaction_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    tx_id = svc.parse_tx_id(transaction_id)
    result = await svc.get_transaction(session, tx_id)
    return ORJSONResponse(content=result)
