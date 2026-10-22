"""Checkout Service business logic: start, scan, complete, read.

The scan path is one CTE round trip (001 research R9) and, once the catalog snapshot is
warm, makes no call to any other service. Completion is the saga in saga.py: stock lives
in Inventory (spec 007), so completing a basket is a coordinated update of two stores.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from checkout_service import catalog_client, repo, saga
from checkout_web.errors import (
    SKU_NOT_FOUND,
    TRANSACTION_NOT_FOUND,
    TRANSACTION_NOT_OPEN,
    ApiError,
)
from checkout_web.money import cents_to_amount

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
    """Runs on an autocommit session (spec 006 R2): the INSERT commits on its own and
    the commit() below is a no-op, kept so the function is correct on either kind."""
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


async def scan_item(session: AsyncSession, tx_id: int, sku: str) -> dict[str, Any]:
    """Returns the ScanResult body.

    Runs on an autocommit session (spec 006 R2): the scan CTE is one atomic statement, so
    rollback()/commit() below are no-ops there. On the not-open path nothing needs
    undoing: the CTE's `WHERE status = 'OPEN'` matched no row.
    """
    # Resolved from the catalog snapshot before any database work, so SKU_NOT_FOUND
    # precedes TRANSACTION_NOT_OPEN (error-catalog.md precedence).
    entry = await catalog_client.get_snapshot().get(sku)
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
        f"Transaction {public_id(tx_id)} is already {str(getattr(status, 'value', status)).lower()}.",
    )


async def complete_transaction(tx_id: int) -> dict[str, Any]:
    return await saga.complete(tx_id)


async def get_transaction(session: AsyncSession, tx_id: int) -> dict[str, Any]:
    row = await repo.get_transaction(session, tx_id)
    if row is None:
        raise ApiError(
            TRANSACTION_NOT_FOUND, f"Transaction {public_id(tx_id)} was not found."
        )
    return {
        "transactionId": public_id(row.id),
        "stationId": row.station_id,
        "status": row.status,
        "itemCount": row.item_count,
        "runningTotal": cents_to_amount(row.running_total_cents),
        "startedAt": row.started_at.isoformat(),
    }
