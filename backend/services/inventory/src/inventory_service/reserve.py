"""Idempotent stock reservation and its compensation (spec 007 research R2).

`reserve` is the spec 006 completion critical section, moved here from Checkout: one
statement locks every basket row in SKU order, checks that each can cover its
quantity, decrements, raises low-stock alerts for threshold crossings, and records
the adjustment keyed by the transaction id. The hot-SKU stock locks are held for that
one statement plus COMMIT, and are gone before Checkout finalizes the transaction.

The adjustment row is the idempotency record: a repeated `reserve` returns the
recorded result without a second decrement, and `release` leaves a record (even when
there was nothing to undo) so a `reserve` that arrives after it is refused.

Neither function commits or begins a transaction. The caller owns the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import orjson
from sqlalchemy import ARRAY, Integer, Text, bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# Lock order: `locked` takes every basket row FOR UPDATE in SKU order, which makes
# deadlock structurally impossible (001 research R10). It is MATERIALIZED and read by
# `ok`, and `dec`/`alerts`/`adj` run under a filter on `ok`, so all locks are granted
# before any row is modified. Under READ COMMITTED, FOR UPDATE and the UPDATE both see
# the latest committed stock, so `ok` and the decrement agree and stock never goes
# negative. When `ok` is false nothing is modified and `short` lists the SKUs that are
# missing or short. The alert rule fires once per descent (001 FR-006). Arrays rather
# than a VALUES list keep one statement text for every basket size.
_RESERVE = text("""
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
adj AS (
  INSERT INTO stock_adjustment (transaction_id, state, lines)
  SELECT :tx_id, 'APPLIED', CAST(:lines AS jsonb) WHERE (SELECT ok FROM ok)
  RETURNING 1
)
SELECT (SELECT ok FROM ok) AS ok,
       (SELECT array_agg(v.sku ORDER BY v.sku) FROM v LEFT JOIN locked l USING (sku)
         WHERE l.sku IS NULL OR l.current_stock < v.qty) AS short
""").bindparams(
    bindparam("skus", type_=ARRAY(Text)),
    bindparam("qtys", type_=ARRAY(Integer)),
)

_GET_ADJUSTMENT = text(
    "SELECT state, lines FROM stock_adjustment WHERE transaction_id = :tx_id"
)

_LOCK_ADJUSTMENT = text(
    "SELECT state, lines FROM stock_adjustment WHERE transaction_id = :tx_id FOR UPDATE"
)

_TOMBSTONE = text(
    "INSERT INTO stock_adjustment (transaction_id, state, lines)"
    " VALUES (:tx_id, 'RELEASED', CAST('[]' AS jsonb))"
    " ON CONFLICT (transaction_id) DO NOTHING RETURNING transaction_id"
)

# Units go back in SKU order too, so a release cannot deadlock against a reserve.
_RESTORE = text("""
WITH v AS (
  SELECT * FROM unnest(CAST(:skus AS text[]), CAST(:qtys AS int[])) AS v(sku, qty)
),
locked AS MATERIALIZED (
  SELECT s.sku FROM inventory_stock s
   WHERE s.sku = ANY(CAST(:skus AS text[])) ORDER BY s.sku FOR UPDATE
)
UPDATE inventory_stock s SET current_stock = s.current_stock + v.qty
  FROM v WHERE s.sku = v.sku AND (SELECT count(*) FROM locked) >= 0
""").bindparams(
    bindparam("skus", type_=ARRAY(Text)),
    bindparam("qtys", type_=ARRAY(Integer)),
)

_MARK_RELEASED = text(
    "UPDATE stock_adjustment SET state = 'RELEASED', updated_at = now()"
    " WHERE transaction_id = :tx_id"
)


@dataclass(frozen=True)
class Reserved:
    replayed: bool


@dataclass(frozen=True)
class Short:
    skus: list[str]


@dataclass(frozen=True)
class AlreadyReleased:
    """A release for this transaction was recorded first; the reservation is refused."""


@dataclass(frozen=True)
class Released:
    units_restored: int
    already_released: bool


def _normalize(lines: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
    merged: dict[str, int] = {}
    for sku, qty in lines:
        merged[sku] = merged.get(sku, 0) + qty
    return sorted(merged.items())


async def _recorded(session: AsyncSession, tx_id: int) -> Reserved | AlreadyReleased | None:
    row = (await session.execute(_GET_ADJUSTMENT, {"tx_id": tx_id})).first()
    if row is None:
        return None
    return Reserved(replayed=True) if row.state == "APPLIED" else AlreadyReleased()


async def reserve(
    session: AsyncSession,
    tx_id: int,
    lines: Sequence[tuple[str, int]],
    threshold: int,
) -> Reserved | Short | AlreadyReleased:
    """Reduce stock for `lines` once for `tx_id`. Call inside the caller's transaction."""
    basket = _normalize(lines)
    if (known := await _recorded(session, tx_id)) is not None:
        return known

    params = {
        "tx_id": tx_id,
        "skus": [sku for sku, _ in basket],
        "qtys": [qty for _, qty in basket],
        "thr": threshold,
        "lines": orjson.dumps([{"sku": sku, "qty": qty} for sku, qty in basket]).decode(),
    }
    try:
        # A savepoint keeps a lost race on `stock_adjustment` from poisoning the
        # caller's transaction.
        async with session.begin_nested():
            res = (await session.execute(_RESERVE, params)).one()
    except IntegrityError:
        # A concurrent reserve or release for the same transaction won the insert.
        known = await _recorded(session, tx_id)
        if known is None:  # pragma: no cover - the row was just seen to exist
            raise
        return known
    if not res.ok:
        return Short(list(res.short or []))
    return Reserved(replayed=False)


async def release(session: AsyncSession, tx_id: int) -> Released:
    """Reverse `tx_id`'s reservation once, or record that none may follow."""
    if (await session.execute(_TOMBSTONE, {"tx_id": tx_id})).first() is not None:
        return Released(units_restored=0, already_released=False)

    row = (await session.execute(_LOCK_ADJUSTMENT, {"tx_id": tx_id})).one()
    if row.state == "RELEASED":
        return Released(units_restored=0, already_released=True)

    lines = _normalize([(e["sku"], int(e["qty"])) for e in row.lines])
    await session.execute(
        _RESTORE,
        {"skus": [sku for sku, _ in lines], "qtys": [qty for _, qty in lines]},
    )
    await session.execute(_MARK_RELEASED, {"tx_id": tx_id})
    return Released(units_restored=sum(qty for _, qty in lines), already_released=False)
