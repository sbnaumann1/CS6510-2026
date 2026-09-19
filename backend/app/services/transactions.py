"""Transaction service: start, scan, complete, read.

The scan path is one CTE round trip (research R9). The completion path is one
explicit DB transaction that takes stock locks in SKU order and applies a single
conditional batched decrement (research R10) — that is what makes the graded
invariant hold under concurrency.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import ARRAY, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog_cache
from app.config import settings
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


_INSERT_TX = text(
    "INSERT INTO transaction (station_id) VALUES (:station_id)"
    " RETURNING id, status, item_count, running_total_cents, started_at"
)

# R9: update the denormalized counters, insert the line, and return both in one
# round trip. `scan_seq` is the global scan sequence feeding the analytics window.
_SCAN = text(
    """
WITH tx AS (
  UPDATE transaction
     SET item_count = item_count + 1,
         running_total_cents = running_total_cents + :price_cents
   WHERE id = :tx_id AND status = 'OPEN'
  RETURNING id, item_count, running_total_cents
), ins AS (
  INSERT INTO transaction_item (transaction_id, sku, unit_price_cents)
  SELECT id, :sku, :price_cents FROM tx
  RETURNING id
)
SELECT tx.item_count, tx.running_total_cents, (SELECT id FROM ins) AS scan_seq
  FROM tx
"""
)

_TX_STATUS = text("SELECT status FROM transaction WHERE id = :tx_id")

_LOCK_TX = text(
    "SELECT status, station_id, started_at FROM transaction"
    " WHERE id = :tx_id FOR UPDATE"
)

_BASKET = text(
    "SELECT sku, count(*) AS qty FROM transaction_item"
    " WHERE transaction_id = :tx_id GROUP BY sku ORDER BY sku"
)

# R10 step 3: one deterministic lock order for every concurrent completion, which
# is what makes deadlock structurally impossible.
_LOCK_STOCK = text(
    "SELECT sku FROM inventory_stock WHERE sku = ANY(:skus) ORDER BY sku FOR UPDATE"
).bindparams(bindparam("skus", type_=ARRAY(Text)))

_COMPLETE_TX = text(
    "UPDATE transaction"
    "   SET status = 'COMPLETED', completed_at = now(), total_amount_cents = :total"
    " WHERE id = :tx_id RETURNING completed_at"
)

_GET_TX = text(
    "SELECT id, station_id, status, item_count, running_total_cents, started_at"
    "  FROM transaction WHERE id = :tx_id"
)


async def start_transaction(session: AsyncSession, station_id: str) -> dict[str, Any]:
    row = (await session.execute(_INSERT_TX, {"station_id": station_id})).one()
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

    row = (
        await session.execute(
            _SCAN, {"tx_id": tx_id, "sku": sku, "price_cents": price_cents}
        )
    ).first()

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
    status = await session.scalar(_TX_STATUS, {"tx_id": tx_id})
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
        tx = (await session.execute(_LOCK_TX, {"tx_id": tx_id})).first()
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
        basket = (await session.execute(_BASKET, {"tx_id": tx_id})).all()
        if not basket:
            raise ApiError(
                EMPTY_BASKET, f"Transaction {public_id(tx_id)} has no scanned items."
            )

        skus = [b.sku for b in basket]

        # 3. Acquire every stock lock in one deterministic order.
        await session.execute(_LOCK_STOCK, {"skus": skus})

        # 4. One conditional batched decrement. The WHERE clause is what
        #    guarantees stock never goes negative; a short count means at least
        #    one SKU could not satisfy its quantity, so the whole thing rolls back.
        values = ", ".join(
            f"(CAST(:sku{i} AS text), CAST(:qty{i} AS integer))"
            for i in range(len(basket))
        )
        params: dict[str, Any] = {"tx_id": tx_id}
        for i, b in enumerate(basket):
            params[f"sku{i}"] = b.sku
            params[f"qty{i}"] = b.qty

        decremented = (
            await session.execute(
                text(
                    "UPDATE inventory_stock s"
                    "   SET current_stock = s.current_stock - v.qty"
                    f"  FROM (VALUES {values}) AS v(sku, qty)"
                    "  WHERE s.sku = v.sku AND s.current_stock >= v.qty"
                    " RETURNING s.sku, s.current_stock"
                ),
                params,
            )
        ).all()

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
        completed_at = await session.scalar(
            _COMPLETE_TX, {"tx_id": tx_id, "total": total_cents}
        )

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
    row = (await session.execute(_GET_TX, {"tx_id": tx_id})).first()
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
