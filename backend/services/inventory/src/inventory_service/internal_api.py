"""Internal reservation routes, called only by Checkout over the mesh
(contracts/internal-apis.md). The gateway never routes /internal/.

Each call is one local transaction in Inventory's own database. Retrying either call
is safe: the adjustment row keyed by the transaction id makes both idempotent.
"""

from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import ORJSONResponse

from checkout_web.errors import INVALID_REQUEST, ApiError
from checkout_web.request import _json_body, log_step
from inventory_service import reserve as reservations
from inventory_service.config import settings
from inventory_service.db import SessionLocal

router = APIRouter(tags=["internal"])

_TX_ID = re.compile(r"^tx-(\d+)$")


def _parse_tx(public_id: Any) -> int:
    m = _TX_ID.match(public_id) if isinstance(public_id, str) else None
    if not m:
        raise ApiError(INVALID_REQUEST, "'transactionId' must look like tx-<number>.")
    return int(m.group(1))


def _parse_epoch(raw: Any) -> int:
    try:
        epoch = int(raw)
    except (TypeError, ValueError):
        epoch = 0
    if epoch < 1 or isinstance(raw, bool):
        raise ApiError(INVALID_REQUEST, "'epoch' must be a positive integer.")
    return epoch


def _parse_lines(raw: Any) -> list[tuple[str, int]]:
    if not isinstance(raw, list) or not raw:
        raise ApiError(INVALID_REQUEST, "'lines' must be a non-empty list.")
    lines: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in raw:
        sku = item.get("sku") if isinstance(item, dict) else None
        qty = item.get("quantity") if isinstance(item, dict) else None
        if not isinstance(sku, str) or not sku or sku in seen:
            raise ApiError(INVALID_REQUEST, "Each line needs a unique non-empty 'sku'.")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise ApiError(INVALID_REQUEST, "Each line needs a positive integer 'quantity'.")
        seen.add(sku)
        lines.append((sku, qty))
    return lines


@router.post("/internal/reservations")
async def reserve_stock(request: Request) -> Response:
    started = time.monotonic()
    body = await _json_body(request, required=True)
    public_id = body.get("transactionId")
    tx_id = _parse_tx(public_id)
    epoch = _parse_epoch(body.get("epoch"))
    lines = _parse_lines(body.get("lines"))

    async with SessionLocal() as session:
        async with session.begin():
            result = await reservations.reserve(
                session, tx_id, lines, settings.low_stock_threshold, epoch=epoch
            )

    def done(outcome: str) -> None:
        log_step("reserve", tx=public_id, outcome=outcome,
                 elapsed_ms=(time.monotonic() - started) * 1000)

    if isinstance(result, reservations.Reserved):
        done("replayed" if result.replayed else "ok")
        return ORJSONResponse(
            content={
                "transactionId": public_id,
                "epoch": epoch,
                "state": "APPLIED",
                "replayed": result.replayed,
            }
        )
    if isinstance(result, reservations.Short):
        done("rejected")
        return ORJSONResponse(
            status_code=409,
            content={
                "error": "INSUFFICIENT_STOCK",
                "message": "Insufficient stock for " + ", ".join(result.skus) + ".",
                "shortSkus": result.skus,
            },
        )
    done("released")
    return ORJSONResponse(
        status_code=409,
        content={
            "error": "RESERVATION_RELEASED",
            "message": f"The reservation for {public_id} was already released.",
        },
    )


@router.delete("/internal/reservations/{transaction_id}")
async def release_stock(transaction_id: str, request: Request) -> Response:
    started = time.monotonic()
    tx_id = _parse_tx(transaction_id)
    epoch = _parse_epoch(request.query_params.get("epoch"))
    async with SessionLocal() as session:
        async with session.begin():
            result = await reservations.release(session, tx_id, epoch=epoch)
    log_step(
        "release",
        tx=transaction_id,
        outcome="replayed" if result.already_released else "ok",
        elapsed_ms=(time.monotonic() - started) * 1000,
    )
    content: dict[str, Any] = {
        "transactionId": transaction_id,
        "epoch": epoch,
        "state": "RELEASED",
        "unitsRestored": result.units_restored,
    }
    if result.already_released:
        content["alreadyReleased"] = True
    return ORJSONResponse(content=content)
