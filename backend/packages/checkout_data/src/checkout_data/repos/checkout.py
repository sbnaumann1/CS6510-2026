"""Data access for the Checkout Service (checkout_service only).

Each function runs one statement against the caller's session and returns what
the database returned. None of them commit, roll back, or begin — the caller
owns the transaction boundary (research R4).
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import ARRAY, Integer, Row, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

# The consuming service; the boundary check (scripts/check_boundaries.py)
# verifies every table below against checkout_data.ownership.TABLES.
SERVICE = "checkout"

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

# The completion's whole critical section in one statement (spec 006 research R1,
# contracts/completion-statement.md). The hot-SKU stock locks are held until COMMIT,
# and every client round trip made while holding them lengthens the hold that all
# other completions queue behind (006 investigation F1); here COMMIT is the only one.
#
# Lock order: `locked` takes every basket row FOR UPDATE in SKU order, which is what
# makes deadlock structurally impossible (001 research R10). It is MATERIALIZED and
# read by `ok`, and `dec`/`fin` run under a one-time filter on `ok`, so all locks are
# granted before any row is modified. Under READ COMMITTED, FOR UPDATE and the UPDATE
# both see the latest committed stock, so `ok` and the decrement agree and stock never
# goes negative. When `ok` is false nothing is modified and `short` lists the SKUs
# that are missing or short. The alert rule fires once per descent (001 FR-006).
# Arrays rather than a VALUES list keep one statement text for every basket size.
_COMPLETE_BASKET = text(
    """
WITH v AS (
  SELECT * FROM unnest(CAST(:skus AS text[]), CAST(:qtys AS int[])) AS v(sku, qty)
),
locked AS MATERIALIZED (
  SELECT s.sku, s.current_stock FROM inventory_stock s
   WHERE s.sku = ANY(CAST(:skus AS text[])) ORDER BY s.sku FOR UPDATE
),
ok AS (
  SELECT count(*) = cardinality(CAST(:skus AS text[]))
         AND coalesce(bool_and(l.current_stock >= v.qty), false) AS ok
    FROM v JOIN locked l USING (sku)
),
dec AS (
  UPDATE inventory_stock s SET current_stock = s.current_stock - v.qty
    FROM v WHERE s.sku = v.sku AND (SELECT ok FROM ok)
  RETURNING s.sku, s.current_stock, v.qty
),
alerts AS (
  INSERT INTO low_stock_alert (sku, current_stock, threshold)
  SELECT sku, current_stock, :thr FROM dec
   WHERE current_stock < :thr AND :thr <= current_stock + qty
  RETURNING 1
),
fin AS (
  UPDATE transaction
     SET status = 'COMPLETED', completed_at = now(), total_amount_cents = :total
   WHERE id = :tx_id AND (SELECT ok FROM ok)
  RETURNING completed_at
)
SELECT (SELECT ok FROM ok) AS ok,
       (SELECT completed_at FROM fin) AS completed_at,
       (SELECT array_agg(v.sku ORDER BY v.sku) FROM v LEFT JOIN locked l USING (sku)
         WHERE l.sku IS NULL OR l.current_stock < v.qty) AS short
"""
).bindparams(
    bindparam("skus", type_=ARRAY(Text)),
    bindparam("qtys", type_=ARRAY(Integer)),
)

_GET_TX = text(
    "SELECT id, station_id, status, item_count, running_total_cents, started_at"
    "  FROM transaction WHERE id = :tx_id"
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


async def complete_basket(
    session: AsyncSession,
    tx_id: int,
    skus: list[str],
    qtys: list[int],
    total_cents: int,
    threshold: int,
) -> Row:
    """Lock, check, decrement, alert and finalize in one statement.

    `skus` ascending and unique, `qtys[i]` the quantity of `skus[i]`; call inside the
    caller's transaction after `lock_transaction`. Returns (ok, completed_at, short):
    when `ok` is false nothing was modified and `short` lists the short SKUs, sorted.
    """
    return (
        await session.execute(
            _COMPLETE_BASKET,
            {
                "tx_id": tx_id,
                "skus": skus,
                "qtys": qtys,
                "total": total_cents,
                "thr": threshold,
            },
        )
    ).one()


async def get_transaction(session: AsyncSession, tx_id: int) -> Row | None:
    return (await session.execute(_GET_TX, {"tx_id": tx_id})).first()


async def sweep_abandoned(session: AsyncSession, minutes: int) -> int:
    result = await session.execute(_SWEEP_ABANDONED, {"mins": minutes})
    return result.rowcount or 0
