"""Transaction service: start, scan, complete, read.

The scan path is one CTE round trip (research R9). The completion path is one
explicit DB transaction that takes stock locks in SKU order and applies a single
conditional batched decrement (research R10) — that is what makes the graded
invariant hold under concurrency.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog_cache
from app.config import settings
from app.db import transactions_repo as repo
from app.errors import (
    EMPTY_BASKET,
    INSUFFICIENT_STOCK,
    SKU_NOT_FOUND,
    TRANSACTION_NOT_FOUND,
    TRANSACTION_NOT_OPEN,
    ApiError,
)
from app.money import cents_to_amount
from app.services import inventory

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
) -> tuple[dict[str, Any], int | None]:
    """Returns (ScanResult body, scan sequence number)."""
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
    return (
        {
            "transactionId": public_id(tx_id),
            "sku": sku,
            "name": name,
            "unitPrice": cents_to_amount(price_cents),
            "itemCount": row.item_count,
            "runningTotal": cents_to_amount(row.running_total_cents),
        },
        row.scan_seq,
    )


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

        skus = [b.sku for b in basket]

        # 3. Acquire every stock lock in one deterministic order.
        await repo.lock_stock(session, skus)

        # 4. One conditional batched decrement. Its WHERE clause is what
        #    guarantees stock never goes negative; a short count means at least
        #    one SKU could not satisfy its quantity, so the whole thing rolls back.
        decremented = await repo.decrement_stock(session, basket)

        if len(decremented) < len(basket):
            short = sorted(set(skus) - {d.sku for d in decremented})
            raise ApiError(
                INSUFFICIENT_STOCK,
                "Insufficient stock for " + ", ".join(short) + ".",
            )

        # 5. Alert rows for any SKU that crossed the threshold on this
        #    completion, inside the same transaction as the decrement (SC-005).
        await inventory.emit_crossings(
            session,
            decremented,
            {b.sku: b.qty for b in basket},
            settings.low_stock_threshold,
        )

        # 6. Finalize.
        total_cents = sum(
            catalog_cache.get(b.sku)[1] * b.qty for b in basket  # type: ignore[index]
        )
        completed_at = await repo.complete_transaction(session, tx_id, total_cents)

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
