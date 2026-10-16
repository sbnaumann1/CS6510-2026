"""Checkout Service business logic: start, scan, complete, read.

The scan path is one CTE round trip (research R9). The completion path is one
explicit DB transaction: lock the transaction row, read the basket, then lock the
stock rows in SKU order, check, decrement, alert and finalize in a single statement
(spec 006 research R1). That keeps the graded invariant under concurrency, and holds
the hot-SKU locks for one client round trip (COMMIT) instead of four or five.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from checkout_data import catalog as catalog_cache
from checkout_data.config import settings
from checkout_data.repos import checkout as repo
from checkout_web.errors import (
    EMPTY_BASKET,
    INSUFFICIENT_STOCK,
    SKU_NOT_FOUND,
    TRANSACTION_NOT_FOUND,
    TRANSACTION_NOT_OPEN,
    ApiError,
)
from checkout_data.money import cents_to_amount

_TX_ID = re.compile(r"^tx-(\d+)$")


def parse_tx_id(public_id: str) -> int:
    """'tx-123' -> 123. Anything else is a 404 without touching the database."""
    m = _TX_ID.match(public_id)
    if not m:
        raise ApiError(TRANSACTION_NOT_FOUND, f"Transaction {public_id} was not found.")
    return int(m.group(1))


def public_id(tx_id: int) -> str:
    return f"tx-{tx_id}"


async def start_transaction(session: AsyncSession, station_id: str) -> dict[str, Any]:
    row = await repo.insert_transaction(session, station_id)
    await session.commit()
    return {
        "transactionId": public_id(row.id),
        "stationId": station_id,
        "status": "OPEN",
        "itemCount": row.item_count,
        "runningTotal": cents_to_amount(row.running_total_cents),
        "startedAt": row.started_at.isoformat(),
    }


async def scan_item(
    session: AsyncSession, tx_id: int, sku: str
) -> dict[str, Any]:
    """Returns the ScanResult body.

    The scan sequence (transaction_item.id) is no longer returned: Analytics
    tails it from the shared database instead of being triggered here.
    """
    # Resolved from memory before any DB work, so SKU_NOT_FOUND precedes
    # TRANSACTION_NOT_OPEN (error-catalog.md precedence).
    entry = catalog_cache.get(sku)
    if entry is None:
        raise ApiError(SKU_NOT_FOUND, f"SKU {sku} was not found.")
    name, price_cents = entry

    row = await repo.scan_item(session, tx_id, sku, price_cents)

    if row is None:
        await session.rollback()
        await _raise_not_open_or_missing(session, tx_id)

    await session.commit()
    return {
        "transactionId": public_id(tx_id),
        "sku": sku,
        "name": name,
        "unitPrice": cents_to_amount(price_cents),
        "itemCount": row.item_count,
        "runningTotal": cents_to_amount(row.running_total_cents),
    }


async def _raise_not_open_or_missing(session: AsyncSession, tx_id: int) -> None:
    """The rare path: decide between 404 and 409 with one cheap query."""
    status = await repo.get_status(session, tx_id)
    if status is None:
        raise ApiError(
            TRANSACTION_NOT_FOUND, f"Transaction {public_id(tx_id)} was not found."
        )
    raise ApiError(
        TRANSACTION_NOT_OPEN,
        f"Transaction {public_id(tx_id)} is already {str(status).lower()}.",
    )


async def complete_transaction(session: AsyncSession, tx_id: int) -> dict[str, Any]:
    async with session.begin():
        # 1. Lock the transaction row. This also serializes two concurrent
        #    completes of the same transaction, so no double decrement (INV-3).
        tx = await repo.lock_transaction(session, tx_id)
        if tx is None:
            raise ApiError(
                TRANSACTION_NOT_FOUND, f"Transaction {public_id(tx_id)} was not found."
            )
        if str(getattr(tx.status, "value", tx.status)) != "OPEN":
            status = str(getattr(tx.status, "value", tx.status)).lower()
            raise ApiError(
                TRANSACTION_NOT_OPEN,
                f"Transaction {public_id(tx_id)} is already {status}.",
            )

        # 2. Collapse the basket to (sku, qty) in SKU order.
        basket = await repo.get_basket(session, tx_id)
        if not basket:
            raise ApiError(
                EMPTY_BASKET, f"Transaction {public_id(tx_id)} has no scanned items."
            )

        # 3-6. One statement: lock every stock row in SKU order, check that each
        #      can cover its quantity, decrement, insert alert rows for threshold
        #      crossings (SC-005) and finalize — or modify nothing (spec 006 R1).
        #      After the locks are granted, only COMMIT waits on a round trip.
        total_cents = sum(
            catalog_cache.get(b.sku)[1] * b.qty for b in basket  # type: ignore[index]
        )
        res = await repo.complete_basket(
            session,
            tx_id,
            [b.sku for b in basket],
            [b.qty for b in basket],
            total_cents,
            settings.low_stock_threshold,
        )
        if not res.ok:
            raise ApiError(
                INSUFFICIENT_STOCK,
                "Insufficient stock for " + ", ".join(res.short) + ".",
            )
        completed_at = res.completed_at

        # 7. Build the receipt from the basket plus the in-memory catalog.
        lines = []
        for b in basket:
            name, price_cents = catalog_cache.get(b.sku)  # type: ignore[misc]
            lines.append(
                {
                    "sku": b.sku,
                    "name": name,
                    "unitPrice": cents_to_amount(price_cents),
                    "quantity": b.qty,
                }
            )

        return {
            "transactionId": public_id(tx_id),
            "stationId": tx.station_id,
            "itemCount": sum(b.qty for b in basket),
            "totalAmount": cents_to_amount(total_cents),
            "startedAt": tx.started_at.isoformat(),
            "completedAt": completed_at.isoformat(),
            "lines": lines,
        }


async def get_transaction(session: AsyncSession, tx_id: int) -> dict[str, Any]:
    row = await repo.get_transaction(session, tx_id)
    if row is None:
        raise ApiError(
            TRANSACTION_NOT_FOUND, f"Transaction {public_id(tx_id)} was not found."
        )
    return {
        "transactionId": public_id(row.id),
        "stationId": row.station_id,
        "status": str(getattr(row.status, "value", row.status)),
        "itemCount": row.item_count,
        "runningTotal": cents_to_amount(row.running_total_cents),
        "startedAt": row.started_at.isoformat(),
    }
