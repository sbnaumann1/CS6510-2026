"""One test per outcome of the completion saga (contracts/completion-saga.md, rows A-H).

Each test injects the failure (at the mesh, or by stopping the saga part-way to model a
crash), then checks what the shopper sees, the final transaction state, the reservation
record, the stock, and that the two stores agree (`assert_consistent`).
"""

from __future__ import annotations

import pytest

from checkout_service import recovery, repo, saga
from checkout_service.db import SessionLocal as CheckoutSessionLocal
from inventory_service import reserve
from inventory_service.db import SessionLocal as InventorySessionLocal
from tests.failure._helpers import (
    adjustment_states,
    assert_consistent,
    basket,
    complete,
    completion_state,
    set_stock,
    status_of,
    stock_of,
    tx_number,
)

HOT = "SKU-000001"


async def test_a_insufficient_stock_is_a_409_and_nothing_changes(client, session):
    await set_stock(session, HOT, 1)
    tx = await basket(client, {HOT: 2})

    r = await complete(client, tx)

    assert r.status_code == 409 and r.json()["error"] == "INSUFFICIENT_STOCK"
    assert await status_of(client, tx) == "OPEN"
    assert await stock_of(HOT) == 1
    assert await adjustment_states(tx) == []
    assert await completion_state(tx) == (1, "ABORTED")
    await assert_consistent()


async def test_b_reserve_never_answers_so_the_saga_rolls_back(client, mesh):
    tx = await basket(client, {HOT: 2})
    mesh.fail("inventory", "/internal/reservations", times=99, mode="timeout", method="POST")

    r = await complete(client, tx)

    assert r.status_code == 503 and r.json()["error"] == "INTERNAL_ERROR"
    assert mesh.calls_to("inventory", "/internal/reservations", "POST") == 3, "retried within budget"
    assert mesh.calls_to("inventory", "/internal/reservations", "DELETE") == 1, "then released"
    assert await status_of(client, tx) == "OPEN"
    assert await stock_of(HOT) == 100
    assert await adjustment_states(tx) == [(1, "RELEASED")], "a tombstone stops a late reserve"
    assert await completion_state(tx) == (1, "ABORTED")
    await assert_consistent()

    # The shopper retries once Inventory is back: a new attempt, a new epoch, success.
    mesh.clear_faults()
    assert (await complete(client, tx)).status_code == 200
    assert await adjustment_states(tx) == [(1, "RELEASED"), (2, "APPLIED")]
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_b_a_slow_reserve_that_recovers_within_the_budget_just_succeeds(client, mesh):
    tx = await basket(client, {HOT: 2})
    mesh.fail("inventory", "/internal/reservations", times=2, mode="timeout", method="POST")

    r = await complete(client, tx)

    assert r.status_code == 200
    assert mesh.calls_to("inventory", "/internal/reservations", "POST") == 3
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_b_a_lost_acknowledgement_is_retried_without_a_second_decrement(client, mesh):
    tx = await basket(client, {HOT: 2})
    mesh.fail("inventory", "/internal/reservations", times=1, mode="drop_response", method="POST")

    r = await complete(client, tx)

    assert r.status_code == 200
    assert mesh.calls_to("inventory", "/internal/reservations", "POST") == 2
    assert await stock_of(HOT) == 98, "applied once although it was asked for twice"
    assert await adjustment_states(tx) == [(1, "APPLIED")]
    await assert_consistent()


async def test_c_a_reservation_released_first_is_refused_and_rolled_back(client):
    tx = await basket(client, {HOT: 2})
    async with InventorySessionLocal() as s:
        async with s.begin():
            await reserve.release(s, tx_number(tx), epoch=1)  # the release won the race

    r = await complete(client, tx)

    assert r.status_code == 503
    assert await status_of(client, tx) == "OPEN"
    assert await stock_of(HOT) == 100
    await assert_consistent()
    assert (await complete(client, tx)).status_code == 200, "the next attempt is a new epoch"
    await assert_consistent()


async def test_d_finalize_fails_after_the_reserve_so_stock_is_given_back(client, monkeypatch):
    tx = await basket(client, {HOT: 3})
    real = saga.finalize_step
    calls = {"n": 0}

    async def flaky(tx_id, total):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        return await real(tx_id, total)

    monkeypatch.setattr(saga, "finalize_step", flaky)

    r = await complete(client, tx)

    assert r.status_code == 503
    assert await status_of(client, tx) == "OPEN"
    assert await stock_of(HOT) == 100, "the reservation was released"
    assert await adjustment_states(tx) == [(1, "RELEASED")]
    await assert_consistent()

    assert (await complete(client, tx)).status_code == 200
    assert await stock_of(HOT) == 97
    await assert_consistent()


async def test_e_an_undeliverable_release_is_finished_by_recovery(client, mesh):
    tx = await basket(client, {HOT: 2})
    mesh.fail("inventory", "/internal/reservations", times=99, mode="timeout", method="POST")
    mesh.fail("inventory", "/internal/reservations", times=99, mode="error", method="DELETE")

    r = await complete(client, tx)

    assert r.status_code == 503
    assert await completion_state(tx) == (1, "COMPENSATING"), "left for recovery"
    assert await status_of(client, tx) == "OPEN", "COMPLETING is reported as OPEN"

    mesh.clear_faults()
    assert await recovery.run_once(lease_s=0.0) == 1
    assert await completion_state(tx) == (1, "ABORTED")
    assert await adjustment_states(tx) == [(1, "RELEASED")]
    await assert_consistent()


async def test_f_a_crash_after_the_reserve_but_before_the_record_is_rolled_back(client):
    tx = await basket(client, {HOT: 2})
    n = tx_number(tx)
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            epoch = await repo.mark_completing(c, n, 170)  # step 1 committed, state RESERVING
    async with InventorySessionLocal() as i:
        async with i.begin():
            await reserve.reserve(i, n, [(HOT, 2)], 50, epoch=epoch)  # step 2 landed
    # ... and Checkout died before recording RESERVED.
    assert await stock_of(HOT) == 98

    assert await recovery.run_once(lease_s=0.0) == 1

    assert await status_of(client, tx) == "OPEN"
    assert await stock_of(HOT) == 100
    assert await completion_state(tx) == (1, "ABORTED")
    await assert_consistent()


async def test_g_a_crash_after_the_reserve_was_recorded_is_rolled_forward(client):
    tx = await basket(client, {HOT: 2})
    n = tx_number(tx)
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            epoch = await repo.mark_completing(c, n, 170)
    async with InventorySessionLocal() as i:
        async with i.begin():
            await reserve.reserve(i, n, [(HOT, 2)], 50, epoch=epoch)
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            await repo.set_completion_state(c, n, "RESERVED")
    # Checkout died before finalizing.

    assert await recovery.run_once(lease_s=0.0) == 1

    assert await status_of(client, tx) == "COMPLETED"
    assert await stock_of(HOT) == 98
    assert await completion_state(tx) == (1, "COMPLETED")
    await assert_consistent()


@pytest.mark.parametrize("fault", ["http500", "error"])
async def test_h_inventory_failing_mid_request_is_a_clean_503(client, mesh, fault):
    tx = await basket(client, {HOT: 2})
    mesh.fail("inventory", "/internal/reservations", times=99, mode=fault, method="POST")

    r = await complete(client, tx)

    assert r.status_code == 503 and r.json()["error"] == "INTERNAL_ERROR"
    assert await status_of(client, tx) == "OPEN"
    assert await stock_of(HOT) == 100
    await assert_consistent()


async def test_a_basket_spanning_several_skus_is_all_or_nothing(client, session):
    await set_stock(session, "SKU-000002", 1)
    tx = await basket(client, {HOT: 2, "SKU-000002": 2})

    r = await complete(client, tx)

    assert r.status_code == 409
    assert await stock_of(HOT) == 100, "the SKU that had enough was not touched either"
    await assert_consistent()
