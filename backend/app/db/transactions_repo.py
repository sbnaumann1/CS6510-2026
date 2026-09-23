"""Data access for the Transactions layer.

Each function runs one statement against the caller's session and returns what
the database returned. None of them commit, roll back, or begin — the caller
owns the transaction boundary (research R4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import ARRAY, Row, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT_TX = text(
    "INSERT INTO transaction (station_id) VALUES (:station_id)"
    " RETURNING id, status, item_count, running_total_cents, started_at"
)

# Update the denormalized counters, insert the line, and return both in one
# round trip (001 research R9). `scan_seq` is the global scan sequence feeding
# the analytics window.
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

# One deterministic lock order for every concurrent completion, which is what
# makes deadlock structurally impossible (001 research R10).
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

_INSERT_ALERT = text(
    "INSERT INTO low_stock_alert (sku, current_stock, threshold)"
    " VALUES (:sku, :current_stock, :threshold)"
)

# Uses the transaction_open_started_idx partial index.
_SWEEP_ABANDONED = text(
    "UPDATE transaction SET status = 'CANCELLED'"
    " WHERE status = 'OPEN'"
    "   AND started_at < now() - make_interval(mins => :mins)"
)


async def insert_transaction(session: AsyncSession, station_id: str) -> Row:
    return (await session.execute(_INSERT_TX, {"station_id": station_id})).one()


async def scan_item(
    session: AsyncSession, tx_id: int, sku: str, price_cents: int
) -> Row | None:
    """None when the transaction is missing or not OPEN."""
    return (
        await session.execute(
            _SCAN, {"tx_id": tx_id, "sku": sku, "price_cents": price_cents}
        )
    ).first()


async def get_status(session: AsyncSession, tx_id: int) -> Any:
    return await session.scalar(_TX_STATUS, {"tx_id": tx_id})


async def lock_transaction(session: AsyncSession, tx_id: int) -> Row | None:
    return (await session.execute(_LOCK_TX, {"tx_id": tx_id})).first()


async def get_basket(session: AsyncSession, tx_id: int) -> Sequence[Row]:
    """(sku, qty) pairs in SKU order."""
    return (await session.execute(_BASKET, {"tx_id": tx_id})).all()


async def lock_stock(session: AsyncSession, skus: list[str]) -> None:
    await session.execute(_LOCK_STOCK, {"skus": skus})


async def decrement_stock(
    session: AsyncSession, basket: Sequence[tuple[str, int]]
) -> Sequence[Row]:
    """One conditional batched decrement; returns (sku, current_stock) for each
    SKU that had enough stock. A short result means at least one did not."""
    values = ", ".join(
        f"(CAST(:sku{i} AS text), CAST(:qty{i} AS integer))" for i in range(len(basket))
    )
    params: dict[str, Any] = {}
    for i, (sku, qty) in enumerate(basket):
        params[f"sku{i}"] = sku
        params[f"qty{i}"] = qty
    return (
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


async def complete_transaction(
    session: AsyncSession, tx_id: int, total_cents: int
) -> datetime:
    return await session.scalar(_COMPLETE_TX, {"tx_id": tx_id, "total": total_cents})


async def get_transaction(session: AsyncSession, tx_id: int) -> Row | None:
    return (await session.execute(_GET_TX, {"tx_id": tx_id})).first()


async def insert_alerts(session: AsyncSession, crossings: list[dict[str, Any]]) -> None:
    """crossings: [{"sku", "current_stock", "threshold"}, ...], non-empty."""
    await session.execute(_INSERT_ALERT, crossings)


async def sweep_abandoned(session: AsyncSession, minutes: int) -> int:
    result = await session.execute(_SWEEP_ABANDONED, {"mins": minutes})
    return result.rowcount or 0
