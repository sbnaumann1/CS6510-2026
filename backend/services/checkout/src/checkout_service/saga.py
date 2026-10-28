"""The completion saga: Checkout orchestrates, Inventory participates
(specs/008-event-driven-kafka/contracts/completion-flow.md; spec 007 research R1-R3).

    1. local tx    OPEN -> COMPLETING, completion RESERVING, request in the outbox  (Checkout's database)
    2. event       publish stock-reservation-requested, await the reply event
    3. (Inventory) one local tx: reserve or reject, outcome in its outbox, publish
    4. local tx    COMPLETING -> COMPLETED, completion COMPLETED                     (Checkout's database)

Roll back when the outcome is unknown, roll forward only when stock is certainly
reserved. Every step is idempotent, so a retried request and the recovery loop can both
drive the same saga to the same end. No database transaction is held while waiting for
the reply, so the hot-SKU stock locks live only inside Inventory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from checkout_service import catalog_snapshot, repo, stock_events
from checkout_service.config import settings
from checkout_service.db import SessionLocal
from checkout_web.errors import (
    EMPTY_BASKET,
    INSUFFICIENT_STOCK,
    INTERNAL_ERROR,
    TRANSACTION_NOT_FOUND,
    TRANSACTION_NOT_OPEN,
    ApiError,
    UpstreamUnavailable,
)
from checkout_web.events import CHECKOUT_COMPLETIONS
from checkout_web.money import cents_to_amount
from checkout_web.request import log_step

ROLLED_BACK = (INTERNAL_ERROR[0], 503)


def public_id(tx_id: int) -> str:
    return f"tx-{tx_id}"


def _status(value: Any) -> str:
    return str(getattr(value, "value", value))


def _lines(basket) -> list[tuple[str, int]]:
    merged: dict[str, int] = {}
    for b in basket:
        merged[b.sku] = merged.get(b.sku, 0) + b.qty
    return sorted(merged.items())


async def _set_state(tx_id: int, state: str, error: str | None = None) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            await repo.set_completion_state(session, tx_id, state, error)


async def _mark_compensating(tx_id: int, event, reason: str | None) -> None:
    """COMPENSATING and the release request it owes Inventory, in one local transaction:
    the release can never be forgotten, and is never announced without the state."""
    async with SessionLocal() as session:
        async with session.begin():
            await repo.set_completion_state(session, tx_id, "COMPENSATING", reason)
            await repo.insert_outbox(session, CHECKOUT_COMPLETIONS, public_id(tx_id), event)


async def reopen(tx_id: int) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            await repo.reopen(session, tx_id)


async def finalize_step(tx_id: int, total_cents: int) -> Any:
    """Step 3. Returns completed_at, or None if the transaction is no longer COMPLETING."""
    async with SessionLocal() as session:
        async with session.begin():
            return await repo.finalize(session, tx_id, total_cents)


async def rollback(tx_id: int, epoch: int, *, reason: str = "") -> None:
    """Compensate: release the reservation, then reopen the transaction.

    If the release cannot be delivered the completion stays COMPENSATING for the recovery
    loop, and UpstreamUnavailable propagates."""
    public = public_id(tx_id)
    t0 = time.monotonic()
    request = stock_events.release_request(public, epoch)
    await _mark_compensating(tx_id, request, reason or None)
    try:
        await stock_events.get_client().release(public, epoch, event=request)
    except UpstreamUnavailable:
        log_step("release", tx=public, outcome="undelivered",
                 elapsed_ms=(time.monotonic() - t0) * 1000)
        raise
    await reopen(tx_id)
    log_step("release", tx=public, outcome="compensated",
             elapsed_ms=(time.monotonic() - t0) * 1000)


@dataclass(frozen=True)
class _Begun:
    tx: Any
    basket: Any
    total_cents: int
    epoch: int
    took_over: bool
    request_event: Any = None  # the reservation request written to the outbox in step 1


@dataclass(frozen=True)
class _Compensating:
    """A previous attempt's rollback was never delivered; its release is still owed."""

    epoch: int


async def _begin(tx_id: int, public: str) -> _Begun | _Compensating:
    """Step 1: decide whether to start, take over or refuse, and record COMPLETING."""
    async with SessionLocal() as session:
        async with session.begin():
            tx = await repo.lock_transaction(session, tx_id)
            if tx is None:
                raise ApiError(TRANSACTION_NOT_FOUND, f"Transaction {public} was not found.")
            status = _status(tx.status)
            if status not in ("OPEN", "COMPLETING"):
                raise ApiError(
                    TRANSACTION_NOT_OPEN, f"Transaction {public} is already {status.lower()}."
                )
            basket = await repo.get_basket(session, tx_id)
            if status == "OPEN":
                if not basket:
                    raise ApiError(EMPTY_BASKET, f"Transaction {public} has no scanned items.")
                total_cents = sum(b.unit_price_cents * b.qty for b in basket)
                epoch = await repo.mark_completing(session, tx_id, total_cents)
                request = stock_events.reservation_request(public, epoch, _lines(basket))
                await repo.insert_outbox(session, CHECKOUT_COMPLETIONS, public, request)
                return _Begun(tx, basket, total_cents, epoch, took_over=False, request_event=request)

            completion = await repo.get_completion(session, tx_id, settings.recovery_lease_s)
            if completion is None:  # pragma: no cover - COMPLETING always has a record
                raise UpstreamUnavailable("checkout", "the completion record is missing")
            if completion.state == "COMPENSATING":
                return _Compensating(completion.epoch)
            if completion.fresh:
                # A live driver is on it: a concurrent duplicate is refused exactly as 005
                # refused the second of two overlapping completes (INV-3).
                raise ApiError(
                    TRANSACTION_NOT_OPEN, f"Transaction {public} is already completing."
                )
            # The driver is gone (its lease expired): this retry takes the saga over. Every
            # step is idempotent, so taking over cannot double-apply anything.
            await repo.set_completion_state(session, tx_id, completion.state)
            return _Begun(tx, basket, completion.total_cents, completion.epoch, took_over=True)


async def complete(tx_id: int) -> dict[str, Any]:
    """POST /transactions/{id}/complete."""
    public = public_id(tx_id)
    started = time.monotonic()

    begun = await _begin(tx_id, public)
    if isinstance(begun, _Compensating):
        # The shopper is retrying after a refusal whose rollback could not be delivered
        # (Inventory was down). Finish that rollback, then start a fresh attempt; if the
        # release still cannot be delivered this is a clean 503 and the transaction is
        # untouched.
        await rollback(tx_id, begun.epoch, reason="finishing an undelivered rollback")
        begun = await _begin(tx_id, public)
        if isinstance(begun, _Compensating):  # pragma: no cover - defensive
            raise UpstreamUnavailable("checkout", "the completion is still being rolled back")
    tx, basket, total_cents, epoch, joined = (
        begun.tx, begun.basket, begun.total_cents, begun.epoch, begun.took_over
    )
    request_event = begun.request_event
    log_step("begin", tx=public, outcome="took-over" if joined else "ok",
             elapsed_ms=(time.monotonic() - started) * 1000)

    # Step 2: ask Inventory to reserve the stock, and wait for its reply event.
    t0 = time.monotonic()
    try:
        reserved = await stock_events.get_client().reserve(
            public, epoch, _lines(basket), event=request_event
        )
    except UpstreamUnavailable as exc:
        log_step("reserve", tx=public, outcome="timeout", elapsed_ms=(time.monotonic() - t0) * 1000)
        await rollback(tx_id, epoch, reason=str(exc))
        raise
    if isinstance(reserved, stock_events.Insufficient):
        await reopen(tx_id)
        log_step("reserve", tx=public, outcome="rejected", elapsed_ms=(time.monotonic() - t0) * 1000)
        raise ApiError(
            INSUFFICIENT_STOCK, "Insufficient stock for " + ", ".join(reserved.short_skus) + "."
        )
    if isinstance(reserved, stock_events.ReservationReleased):
        # A release for this transaction was recorded first: it is already undone.
        await reopen(tx_id)
        log_step("reserve", tx=public, outcome="released", elapsed_ms=(time.monotonic() - t0) * 1000)
        raise ApiError(ROLLED_BACK, "The completion was rolled back; retry.")
    log_step("reserve", tx=public, outcome="ok", elapsed_ms=(time.monotonic() - t0) * 1000)
    await _set_state(tx_id, "RESERVED")

    # Step 3: finalize locally.
    t0 = time.monotonic()
    try:
        completed_at = await finalize_step(tx_id, total_cents)
    except Exception as exc:  # database failure after stock was reserved
        log_step("finalize", tx=public, outcome="error", elapsed_ms=(time.monotonic() - t0) * 1000)
        await rollback(tx_id, epoch, reason=f"finalize failed: {type(exc).__name__}")
        raise ApiError(ROLLED_BACK, "Completion failed and was rolled back; retry.") from exc
    if completed_at is None:
        # Another driver finished it, or rolled it back, while this call was in flight.
        async with SessionLocal() as session:
            now_status = _status(await repo.get_status(session, tx_id))
            if now_status != "COMPLETED":
                raise ApiError(ROLLED_BACK, "The completion was rolled back; retry.")
            completed_at = await repo.get_completed_at(session, tx_id)
    log_step("finalize", tx=public, outcome="ok", elapsed_ms=(time.monotonic() - t0) * 1000)

    snapshot = catalog_snapshot.get_snapshot()
    if basket:
        try:
            await snapshot.get(basket[0].sku)  # warm the names after a restart
        except UpstreamUnavailable:
            pass  # names fall back to the SKU; the receipt is still correct
    return {
        "transactionId": public,
        "stationId": tx.station_id,
        "itemCount": sum(b.qty for b in basket),
        "totalAmount": cents_to_amount(total_cents),
        "startedAt": tx.started_at.isoformat(),
        "completedAt": completed_at.isoformat(),
        "lines": [
            {
                "sku": b.sku,
                "name": snapshot.name_of(b.sku),
                "unitPrice": cents_to_amount(b.unit_price_cents),
                "quantity": b.qty,
            }
            for b in basket
        ],
    }
