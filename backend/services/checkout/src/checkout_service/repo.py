"""Data access for the Checkout Service.

Each function runs one statement against the caller's session and returns what the
database returned. None of them commit, roll back, or begin: the caller owns the
transaction boundary (001/002 research R4). Stock is not here: Inventory owns it
(spec 007), and the completion statement that used to lock stock rows moved there.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT_TX = text(
    "INSERT INTO transaction (station_id) VALUES (:station_id)"
    " RETURNING id, status, item_count, running_total_cents, started_at"
)

# Update the denormalized counters, insert the line, and return both in one round trip
# (001 research R9). `scan_seq` is the global scan sequence that Analytics reads.
_SCAN = text("""
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
""")

_TX_STATUS = text("SELECT status FROM transaction WHERE id = :tx_id")

_LOCK_TX = text(
    "SELECT status, station_id, started_at FROM transaction"
    " WHERE id = :tx_id FOR UPDATE"
)

# The basket with the price captured at scan time, so the total and the receipt never
# need Catalog (spec 007 research R6).
_BASKET = text(
    "SELECT sku, unit_price_cents, count(*) AS qty FROM transaction_item"
    " WHERE transaction_id = :tx_id GROUP BY sku, unit_price_cents"
    " ORDER BY sku, unit_price_cents"
)

# COMPLETING is internal; the public API reports it as OPEN.
_GET_TX = text(
    "SELECT id, station_id,"
    "       CASE WHEN status = 'COMPLETING' THEN 'OPEN' ELSE status::text END AS status,"
    "       item_count, running_total_cents, started_at"
    "  FROM transaction WHERE id = :tx_id"
)

# Uses the transaction_open_started_idx partial index. COMPLETING rows are never swept.
_SWEEP_ABANDONED = text(
    "UPDATE transaction SET status = 'CANCELLED'"
    " WHERE status = 'OPEN'"
    "   AND started_at < now() - make_interval(mins => :mins)"
)

# --- completion saga (contracts/completion-saga.md) -------------------------------

_MARK_COMPLETING = text("UPDATE transaction SET status = 'COMPLETING' WHERE id = :tx_id")

# Starting a saga: the first attempt is epoch 1, and every later attempt on the same
# transaction (after a rollback) is the next epoch.
_UPSERT_COMPLETION = text(
    "INSERT INTO completion (transaction_id, epoch, state, total_cents)"
    " VALUES (:tx_id, 1, 'RESERVING', :total)"
    " ON CONFLICT (transaction_id) DO UPDATE"
    "   SET state = 'RESERVING', total_cents = EXCLUDED.total_cents,"
    "       epoch = completion.epoch + 1,"
    "       attempts = 0, updated_at = now(), last_error = NULL"
    " RETURNING epoch"
)

# `fresh`: a live driver touched this completion within the lease, so it is in flight.
_GET_COMPLETION = text(
    "SELECT state, epoch, total_cents, attempts,"
    "       updated_at > now() - make_interval(secs => :lease) AS fresh"
    "  FROM completion WHERE transaction_id = :tx_id"
)

_SET_COMPLETION_STATE = text(
    "UPDATE completion SET state = :state, updated_at = now(),"
    "       last_error = coalesce(:err, last_error)"
    " WHERE transaction_id = :tx_id AND state NOT IN ('COMPLETED', 'ABORTED')"
)

# Step 3: guarded by status = 'COMPLETING', so a second driver (a retried request or the
# recovery loop) finds the work already done, and a completion that was rolled back
# elsewhere is not finalized behind its back.
_FINALIZE = text("""
WITH fin AS (
    UPDATE transaction
       SET status = 'COMPLETED', completed_at = now(), total_amount_cents = :total
     WHERE id = :tx_id AND status = 'COMPLETING'
    RETURNING completed_at
), c AS (
    UPDATE completion SET state = 'COMPLETED', updated_at = now()
     WHERE transaction_id = :tx_id AND EXISTS (SELECT 1 FROM fin)
)
SELECT completed_at FROM fin
""")

# Roll back: the transaction is open again and the saga's record is closed. The row stays so
# the next attempt gets the next epoch.
_REOPEN = text("""
WITH t AS (
    UPDATE transaction SET status = 'OPEN'
     WHERE id = :tx_id AND status = 'COMPLETING'
    RETURNING id
)
UPDATE completion SET state = 'ABORTED', updated_at = now()
 WHERE transaction_id = :tx_id AND state NOT IN ('COMPLETED', 'ABORTED')
   AND EXISTS (SELECT 1 FROM t)
""")

# Claim stuck completions by bumping their lease. SKIP LOCKED lets several Checkout
# replicas run the loop; the caller does its HTTP work after this statement commits.
_CLAIM_STUCK = text("""
UPDATE completion SET updated_at = now(), attempts = attempts + 1
 WHERE transaction_id IN (
        SELECT transaction_id FROM completion
         WHERE state NOT IN ('COMPLETED', 'ABORTED')
           AND updated_at < now() - make_interval(secs => :lease)
         ORDER BY updated_at LIMIT :n FOR UPDATE SKIP LOCKED)
RETURNING transaction_id, epoch, state, total_cents
""")

# --- scan log read by Analytics, and the verification reports -----------------------

_SCANS_AFTER = text(
    "SELECT id, sku FROM transaction_item WHERE id > :after ORDER BY id LIMIT :limit"
)
_MAX_SCAN_ID = text("SELECT coalesce(max(id), 0) FROM transaction_item")
_SCAN_COUNT = text("SELECT count(*) FROM transaction_item")
_LOG_TOKEN = text("SELECT token FROM scan_log WHERE id = 1")

_SOLD_BY_SKU = text(
    "SELECT i.sku, count(*) AS sold FROM transaction_item i"
    "  JOIN transaction t ON t.id = i.transaction_id"
    " WHERE t.status = 'COMPLETED' GROUP BY i.sku ORDER BY i.sku"
)
_COMPLETED_IDS = text("SELECT id FROM transaction WHERE status = 'COMPLETED' ORDER BY id")
_COMPLETING_COUNT = text("SELECT count(*) FROM transaction WHERE status = 'COMPLETING'")
_OPEN_COMPLETIONS = text(
    "SELECT count(*) FROM completion WHERE state NOT IN ('COMPLETED', 'ABORTED')"
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
    """(sku, unit_price_cents, qty) rows in SKU order."""
    return (await session.execute(_BASKET, {"tx_id": tx_id})).all()


async def get_transaction(session: AsyncSession, tx_id: int) -> Row | None:
    return (await session.execute(_GET_TX, {"tx_id": tx_id})).first()


async def get_completed_at(session: AsyncSession, tx_id: int) -> Any:
    return await session.scalar(
        text("SELECT completed_at FROM transaction WHERE id = :tx_id"), {"tx_id": tx_id}
    )


async def sweep_abandoned(session: AsyncSession, minutes: int) -> int:
    result = await session.execute(_SWEEP_ABANDONED, {"mins": minutes})
    return result.rowcount or 0


async def mark_completing(session: AsyncSession, tx_id: int, total_cents: int) -> int:
    """Start a saga and return its epoch."""
    await session.execute(_MARK_COMPLETING, {"tx_id": tx_id})
    return int(
        await session.scalar(_UPSERT_COMPLETION, {"tx_id": tx_id, "total": total_cents})
    )


async def get_completion(session: AsyncSession, tx_id: int, lease_s: float) -> Row | None:
    return (
        await session.execute(_GET_COMPLETION, {"tx_id": tx_id, "lease": lease_s})
    ).first()


async def set_completion_state(
    session: AsyncSession, tx_id: int, state: str, error: str | None = None
) -> None:
    await session.execute(
        _SET_COMPLETION_STATE, {"tx_id": tx_id, "state": state, "err": error}
    )


async def finalize(session: AsyncSession, tx_id: int, total_cents: int) -> Any:
    """completed_at, or None when the transaction is no longer COMPLETING."""
    return await session.scalar(_FINALIZE, {"tx_id": tx_id, "total": total_cents})


async def reopen(session: AsyncSession, tx_id: int) -> None:
    await session.execute(_REOPEN, {"tx_id": tx_id})


async def claim_stuck(session: AsyncSession, lease_s: float, limit: int = 20) -> Sequence[Row]:
    return (await session.execute(_CLAIM_STUCK, {"lease": lease_s, "n": limit})).all()


async def scans_after(session: AsyncSession, after: int, limit: int) -> Sequence[Row]:
    return (await session.execute(_SCANS_AFTER, {"after": after, "limit": limit})).all()


async def max_scan_id(session: AsyncSession) -> int:
    return int(await session.scalar(_MAX_SCAN_ID) or 0)


async def log_token(session: AsyncSession) -> str:
    return await session.scalar(_LOG_TOKEN) or ""


async def scan_count(session: AsyncSession) -> int:
    return int(await session.scalar(_SCAN_COUNT) or 0)


async def sold_by_sku(session: AsyncSession) -> Sequence[Row]:
    return (await session.execute(_SOLD_BY_SKU)).all()


async def completed_ids(session: AsyncSession) -> list[int]:
    return [r.id for r in (await session.execute(_COMPLETED_IDS)).all()]


async def completing_count(session: AsyncSession) -> int:
    return int(await session.scalar(_COMPLETING_COUNT) or 0)


async def open_completions(session: AsyncSession) -> int:
    return int(await session.scalar(_OPEN_COMPLETIONS) or 0)
