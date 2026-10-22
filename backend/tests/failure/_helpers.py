"""Shared helpers for the failure-injection tests (FR-023, SC-003, SC-004).

`assert_consistent` runs the same checks as scripts/verify_invariant.py (data-model.md §6)
against the two test databases: after any outcome, however a step failed, the stores must
agree.
"""

from __future__ import annotations

from sqlalchemy import text

from checkout_service.db import SessionLocal as CheckoutSessionLocal
from inventory_service.db import SessionLocal as InventorySessionLocal

_SOLD = text(
    "SELECT i.sku, count(*) AS sold FROM transaction_item i"
    "  JOIN transaction t ON t.id = i.transaction_id"
    " WHERE t.status = 'COMPLETED' GROUP BY i.sku"
)
_COMPLETED = text("SELECT id FROM transaction WHERE status = 'COMPLETED'")
_COMPLETING = text("SELECT count(*) FROM transaction WHERE status = 'COMPLETING'")
_OPEN_COMPLETIONS = text(
    "SELECT count(*) FROM completion WHERE state NOT IN ('COMPLETED', 'ABORTED')"
)
_STOCK = text("SELECT sku, initial_stock, current_stock FROM inventory_stock")
_ADJUSTMENTS = text("SELECT transaction_id, epoch, state FROM stock_adjustment")


async def assert_consistent() -> None:
    async with CheckoutSessionLocal() as c, InventorySessionLocal() as i:
        sold = {r.sku: r.sold for r in (await c.execute(_SOLD)).all()}
        completed = {r.id for r in (await c.execute(_COMPLETED)).all()}
        completing = await c.scalar(_COMPLETING)
        open_completions = await c.scalar(_OPEN_COMPLETIONS)
        stock = (await i.execute(_STOCK)).all()
        adjustments = (await i.execute(_ADJUSTMENTS)).all()

    assert completing == 0, f"{completing} transaction(s) stuck COMPLETING"
    assert open_completions == 0, f"{open_completions} completion(s) not in a terminal state"
    for row in stock:
        assert row.current_stock >= 0, f"negative stock for {row.sku}"
        assert row.initial_stock - row.current_stock == sold.get(row.sku, 0), (
            f"{row.sku}: stock down {row.initial_stock - row.current_stock}, "
            f"sold {sold.get(row.sku, 0)}"
        )
    applied = [a.transaction_id for a in adjustments if a.state == "APPLIED"]
    assert len(applied) == len(set(applied)), "a transaction has two APPLIED reservations"
    assert set(applied) == completed, (
        f"APPLIED reservations {sorted(applied)} != COMPLETED transactions {sorted(completed)}"
    )


async def basket(client, items: dict[str, int], station: str = "station-01") -> str:
    """Open a transaction and scan `items` ({sku: units}); returns its public id."""
    tx = (await client.post("/transactions", json={"stationId": station})).json()["transactionId"]
    for sku, units in items.items():
        for _ in range(units):
            r = await client.post(f"/transactions/{tx}/items", json={"sku": sku})
            assert r.status_code == 200, r.text
    return tx


async def complete(client, tx: str):
    return await client.post(f"/transactions/{tx}/complete", json={})


async def status_of(client, tx: str) -> str:
    return (await client.get(f"/transactions/{tx}")).json()["status"]


def tx_number(tx: str) -> int:
    return int(tx.removeprefix("tx-"))


async def set_stock(session, sku: str, units: int) -> None:
    """Set stock (and its starting value, so the invariant still holds)."""
    await session.execute(
        text(
            "UPDATE inventory_stock SET current_stock = :n, initial_stock = :n WHERE sku = :sku"
        ),
        {"n": units, "sku": sku},
    )
    await session.commit()


async def stock_of(sku: str) -> int:
    async with InventorySessionLocal() as i:
        return await i.scalar(
            text("SELECT current_stock FROM inventory_stock WHERE sku = :sku"), {"sku": sku}
        )


async def adjustment_states(tx: str) -> list[tuple[int, str]]:
    async with InventorySessionLocal() as i:
        rows = (
            await i.execute(
                text(
                    "SELECT epoch, state FROM stock_adjustment"
                    " WHERE transaction_id = :tx ORDER BY epoch"
                ),
                {"tx": tx_number(tx)},
            )
        ).all()
    return [(r.epoch, r.state) for r in rows]


async def completion_state(tx: str) -> tuple[int, str] | None:
    async with CheckoutSessionLocal() as c:
        row = (
            await c.execute(
                text("SELECT epoch, state FROM completion WHERE transaction_id = :tx"),
                {"tx": tx_number(tx)},
            )
        ).first()
    return None if row is None else (row.epoch, row.state)
