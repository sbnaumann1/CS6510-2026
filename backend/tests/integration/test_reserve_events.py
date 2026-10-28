"""Inventory's reserve and release are idempotent and race-safe (contracts/completion-saga.md
"Idempotency rules", research R2), tested against Inventory's own database."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from inventory_service import reserve
from inventory_service.db import SessionLocal


async def do_reserve(tx_id, lines, threshold=50):
    return await reserve.reserve_once(SessionLocal, tx_id, lines, threshold)


async def do_release(tx_id):
    async with SessionLocal() as s:
        async with s.begin():
            return await reserve.release(s, tx_id)


async def stock(sku="SKU-000001") -> int:
    async with SessionLocal() as s:
        return await s.scalar(
            text("SELECT current_stock FROM inventory_stock WHERE sku = :sku"), {"sku": sku}
        )


async def test_reserving_twice_reduces_stock_once(reset_db):
    first = await do_reserve(1, [("SKU-000001", 3)])
    second = await do_reserve(1, [("SKU-000001", 3)])
    assert first == reserve.Reserved(replayed=False)
    assert second == reserve.Reserved(replayed=True)
    assert await stock() == 97


async def test_insufficient_stock_changes_nothing_and_records_the_refusal(reset_db):
    result = await do_reserve(2, [("SKU-000001", 5), ("SKU-000002", 101)])
    assert result == reserve.Short(["SKU-000002"])
    assert await stock() == 100
    async with SessionLocal() as s:
        states = [r.state for r in (await s.execute(text("SELECT state FROM stock_adjustment"))).all()]
    assert states == ["REJECTED"], "spec 008: the refusal is recorded, so it replays"
    # ...and a repeat replays the same refusal, even if stock has since been replenished.
    async with SessionLocal() as s:
        await s.execute(text("UPDATE inventory_stock SET current_stock = 500 WHERE sku = 'SKU-000002'"))
        await s.commit()
    assert await do_reserve(2, [("SKU-000001", 5), ("SKU-000002", 101)]) == reserve.Short(["SKU-000002"])
    assert await stock() == 100, "the replayed refusal took nothing"


async def test_unknown_sku_is_short(reset_db):
    assert await do_reserve(3, [("SKU-999999", 1)]) == reserve.Short(["SKU-999999"])


async def test_release_restores_units_once(reset_db):
    await do_reserve(4, [("SKU-000001", 4), ("SKU-000002", 2)])
    assert await stock() == 96
    first = await do_release(4)
    again = await do_release(4)
    assert first == reserve.Released(units_restored=6, already_released=False)
    assert again == reserve.Released(units_restored=0, already_released=True)
    assert await stock() == 100
    assert await stock("SKU-000002") == 100


async def test_release_before_reserve_leaves_a_tombstone_that_refuses_the_reserve(reset_db):
    tombstone = await do_release(5)
    assert tombstone == reserve.Released(units_restored=0, already_released=False)
    assert await do_reserve(5, [("SKU-000001", 1)]) == reserve.AlreadyReleased()
    assert await stock() == 100


async def test_concurrent_reserves_of_one_transaction_decrement_once(reset_db):
    results = await asyncio.gather(*(do_reserve(6, [("SKU-000001", 2)]) for _ in range(6)))
    assert sum(1 for r in results if r == reserve.Reserved(replayed=False)) == 1
    assert all(isinstance(r, reserve.Reserved) for r in results)
    assert await stock() == 98


async def test_release_racing_a_reserve_never_leaves_stock_taken(reset_db):
    # Whichever lands first, the end state is consistent: released, with all units back.
    reserve_result, release_result = await asyncio.gather(
        do_reserve(7, [("SKU-000001", 5)]), do_release(7)
    )
    assert isinstance(release_result, reserve.Released)
    assert await stock() == 100
    async with SessionLocal() as s:
        state = await s.scalar(
            text("SELECT state FROM stock_adjustment WHERE transaction_id = 7")
        )
    assert state == "RELEASED"
    assert reserve_result in (reserve.Reserved(replayed=False), reserve.AlreadyReleased())


async def test_many_concurrent_reserves_of_the_hot_sku_never_oversell(reset_db):
    results = await asyncio.gather(
        *(do_reserve(100 + i, [("SKU-000001", 1)]) for i in range(150))
    )
    won = [r for r in results if r == reserve.Reserved(replayed=False)]
    lost = [r for r in results if isinstance(r, reserve.Short)]
    assert len(won) == 100 and len(lost) == 50
    assert await stock() == 0


async def test_overlapping_baskets_in_opposite_orders_do_not_deadlock(reset_db):
    # Rows are locked in SKU order whatever the order the caller lists them in.
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                do_reserve(
                    200 + i,
                    [("SKU-000001", 1), ("SKU-000002", 1)] if i % 2 else [("SKU-000002", 1), ("SKU-000001", 1)],
                )
                for i in range(40)
            )
        ),
        20,
    )
    assert all(isinstance(r, reserve.Reserved) for r in results)
    assert await stock() == 60 and await stock("SKU-000002") == 60


async def test_the_alert_fires_once_per_descent(reset_db):
    async with SessionLocal() as s:
        await s.execute(text("UPDATE inventory_stock SET current_stock = 52 WHERE sku = 'SKU-000001'"))
        await s.commit()
    for tx in (300, 301, 302, 303):
        await do_reserve(tx, [("SKU-000001", 1)])  # 52 -> 51 -> 50 -> 49 (crosses) -> 48
    async with SessionLocal() as s:
        n = await s.scalar(text("SELECT count(*) FROM low_stock_alert WHERE sku = 'SKU-000001'"))
    assert n == 1
