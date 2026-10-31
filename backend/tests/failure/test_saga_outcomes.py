"""One test per outcome of the completion flow (specs/008-event-driven-kafka/contracts/
completion-flow.md, rows A-K).

Each test injects the failure on the event bus (drop, duplicate, delay, a failed publish, a
service that is stopped) or by stopping the saga part-way to model a crash, then checks what
the shopper sees, the final transaction state, the reservation record, the stock, and that the
two stores agree (`assert_consistent`).
"""

from __future__ import annotations

import asyncio

import pytest

from checkout_service import outbox as checkout_outbox
from checkout_service import recovery, repo, saga
from checkout_service.db import SessionLocal as CheckoutSessionLocal
from checkout_web import events as ev
from inventory_service import reserve
from inventory_service.db import SessionLocal as InventorySessionLocal
from tests.failure._helpers import (
    RELEASE_REQUESTS,
    REPLIES,
    RESERVATION_REQUESTS,
    adjustment_states,
    adjustment_states_are,
    assert_consistent,
    basket,
    complete,
    completion_state,
    of_type,
    set_stock,
    status_of,
    stock_of,
    tx_number,
    wait_until,
)

HOT = "SKU-000001"


async def test_a_insufficient_stock_is_a_409_and_nothing_changes(client, session):
    await set_stock(session, HOT, 1)
    tx = await basket(client, {HOT: 2})

    r = await complete(client, tx)

    assert r.status_code == 409 and r.json()["error"] == "INSUFFICIENT_STOCK"
    assert await status_of(client, tx) == "OPEN"
    assert await stock_of(HOT) == 1
    assert await adjustment_states(tx) == [(1, "REJECTED")], "the refusal is recorded; no stock moved"
    assert await completion_state(tx) == (1, "ABORTED")
    await assert_consistent()


async def test_b_the_request_never_arrives_so_the_flow_rolls_back(client, stack):
    tx = await basket(client, {HOT: 2})
    stack.bus.drop(RESERVATION_REQUESTS, times=99)  # Inventory never hears it

    r = await complete(client, tx)

    assert r.status_code == 503 and r.json()["error"] == "INTERNAL_ERROR"
    assert stack.bus.attempts_of(ev.STOCK_RESERVATION_REQUESTED) == 3, "the request, then two re-publishes"
    assert stack.bus.attempts_of(ev.STOCK_RELEASE_REQUESTED) == 1, "then a release"
    assert await status_of(client, tx) == "OPEN"
    assert await stock_of(HOT) == 100
    assert await adjustment_states(tx) == [(1, "RELEASED")], "a tombstone stops a late reserve"
    assert await completion_state(tx) == (1, "ABORTED")
    await assert_consistent()

    # The shopper retries once the path is healthy: a new attempt, a new epoch, success.
    stack.bus.clear_faults()
    assert (await complete(client, tx)).status_code == 200
    assert await adjustment_states(tx) == [(1, "RELEASED"), (2, "APPLIED")]
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_b_a_slow_start_that_recovers_within_the_budget_just_succeeds(client, stack):
    tx = await basket(client, {HOT: 2})
    stack.bus.drop(RESERVATION_REQUESTS, times=2)  # the first two copies are lost, the third arrives

    r = await complete(client, tx)

    assert r.status_code == 200
    assert stack.bus.attempts_of(ev.STOCK_RESERVATION_REQUESTED) == 3
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_b_a_lost_reply_is_asked_again_without_a_second_decrement(client, stack):
    tx = await basket(client, {HOT: 2})
    stack.bus.drop(of_type(ev.STOCK_RESERVED), times=1)  # Inventory applied it; the reply is lost

    r = await complete(client, tx)

    assert r.status_code == 200
    assert stack.bus.attempts_of(ev.STOCK_RESERVATION_REQUESTED) == 2, "asked again after the first wait"
    assert await stock_of(HOT) == 98, "applied once although it was asked for twice"
    assert await adjustment_states(tx) == [(1, "APPLIED")]
    replies = stack.bus.events(ev.INVENTORY_STOCK)
    assert [e.data.get("replayed") for e in replies] == [True], "the second answer replays the record"
    await assert_consistent()


async def test_c_a_reservation_released_first_is_refused_and_rolled_back(client, stack):
    tx = await basket(client, {HOT: 2})
    async with InventorySessionLocal() as s:
        async with s.begin():
            await reserve.release(s, tx_number(tx), epoch=1)  # the release won the race

    r = await complete(client, tx)

    assert r.status_code == 503
    assert [e.type for e in stack.bus.events(ev.INVENTORY_STOCK)] == [ev.RESERVATION_REFUSED]
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


async def test_e_inventory_down_leaves_a_rollback_that_recovery_finishes(client, stack):
    tx = await basket(client, {HOT: 2})
    await stack.stop_service("inventory")

    r = await complete(client, tx)

    assert r.status_code == 503
    assert await completion_state(tx) == (1, "COMPENSATING"), "the release is owed, not forgotten"
    assert await status_of(client, tx) == "OPEN", "COMPLETING is reported as OPEN"

    await stack.start_service("inventory")  # it finds its backlog: the request, then the release
    await wait_until(lambda: adjustment_states_are(tx, [(1, "RELEASED")]), what="Inventory's backlog")
    assert await stock_of(HOT) == 100

    assert await recovery.run_once(lease_s=0.0) == 1
    assert await completion_state(tx) == (1, "ABORTED")
    assert await adjustment_states(tx) == [(1, "RELEASED")]
    await assert_consistent()


async def test_e_a_retry_finishes_an_undelivered_rollback_then_completes(client, stack):
    """Inventory was down, so the release could not be confirmed. When it is back the
    shopper's plain retry must work at once, not wait for the recovery loop."""
    tx = await basket(client, {HOT: 2})
    await stack.stop_service("inventory")
    assert (await complete(client, tx)).status_code == 503
    assert await completion_state(tx) == (1, "COMPENSATING")

    await stack.start_service("inventory")  # Inventory is back
    r = await complete(client, tx)

    assert r.status_code == 200
    assert await completion_state(tx) == (2, "COMPLETED"), "a fresh attempt, a new epoch"
    assert await adjustment_states(tx) == [(1, "RELEASED"), (2, "APPLIED")]
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_e_a_retry_while_inventory_is_still_down_is_a_clean_503(client, stack):
    tx = await basket(client, {HOT: 2})
    await stack.stop_service("inventory")
    assert (await complete(client, tx)).status_code == 503

    r = await complete(client, tx)  # Inventory still down
    assert r.status_code == 503 and r.json()["error"] == "INTERNAL_ERROR"
    assert await status_of(client, tx) == "OPEN"

    await stack.start_service("inventory")
    await wait_until(lambda: adjustment_states_are(tx, [(1, "RELEASED")]), what="Inventory's backlog")
    assert await recovery.run_once(lease_s=0.0) == 1
    assert await stock_of(HOT) == 100
    await assert_consistent()


async def test_f_a_crash_after_the_reserve_but_before_the_record_is_rolled_back(client):
    tx = await basket(client, {HOT: 2})
    n = tx_number(tx)
    async with CheckoutSessionLocal() as c:
        async with c.begin():
            epoch = await repo.mark_completing(c, n, 170)  # step 1 committed, state RESERVING
    async with InventorySessionLocal() as i:
        async with i.begin():
            await reserve.reserve(i, n, [(HOT, 2)], 50, epoch=epoch)  # the reservation landed
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


async def test_h_inventory_failing_mid_request_is_retried_and_applies_once(client, stack, monkeypatch):
    """The database fails under Inventory's handler once. The consumer waits and retries
    (the request is not lost, and not dead-lettered), and the shopper still succeeds."""
    from sqlalchemy.exc import OperationalError

    from inventory_service import handlers

    real = handlers.reservations.reserve
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("stmt", {}, Exception("connection lost"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(handlers.reservations, "reserve", flaky)
    tx = await basket(client, {HOT: 2})

    r = await complete(client, tx)

    assert r.status_code == 200
    assert await stock_of(HOT) == 98, "applied once"
    assert stock_of_dead_letters(stack) == 0
    await assert_consistent()


async def test_h_inventory_crashing_before_its_commit_is_redelivered_and_applies_once(client, stack):
    stack.bus.crash_before_commit("inventory", times=1)  # handled, but the offset commit is lost
    tx = await basket(client, {HOT: 2})

    r = await complete(client, tx)

    assert r.status_code == 200
    assert await stock_of(HOT) == 98, "the redelivered request was recognised, not applied again"
    assert await adjustment_states(tx) == [(1, "APPLIED")]
    await assert_consistent()


async def test_i_the_broker_refusing_the_request_is_delivered_by_the_sweeper(client, stack):
    """The request is committed in the outbox even though the broker refuses the publish; the
    sweeper delivers it, within the shopper's wait."""
    stack.bus.fail_publish(times=1, predicate=RESERVATION_REQUESTS)
    tx = await basket(client, {HOT: 2})

    pending = asyncio.create_task(complete(client, tx))
    await asyncio.sleep(0.1)
    assert await checkout_outbox.sweep_once(age_s=0.0) == 1, "the committed request was still unpublished"
    r = await pending

    assert r.status_code == 200
    assert await stock_of(HOT) == 98
    await assert_consistent()


async def test_i_a_broker_outage_through_the_deadline_is_a_503_and_nothing_is_lost(client, stack):
    stack.bus.fail_publish(times=99, predicate=RESERVATION_REQUESTS)
    tx = await basket(client, {HOT: 2})

    r = await complete(client, tx)

    assert r.status_code == 503
    assert await status_of(client, tx) == "OPEN"
    assert await completion_state(tx) == (1, "ABORTED"), "the release was confirmed, so the rollback finished"
    assert await stock_of(HOT) == 100

    # The broker returns: the reservation request that was never published is swept out now,
    # after the release it was overtaken by. Inventory refuses it: no stock moves.
    stack.bus.clear_faults()
    assert await checkout_outbox.sweep_once(age_s=0.0) >= 1
    await wait_until(
        lambda: any(e.type == ev.RESERVATION_REFUSED for e in stack.bus.events(ev.INVENTORY_STOCK)),
        what="the late request being refused",
    )
    assert await stock_of(HOT) == 100
    assert await adjustment_states(tx) == [(1, "RELEASED")]
    await assert_consistent()


async def test_j_duplicate_replies_are_ignored(client, stack):
    stack.bus.duplicate(REPLIES, copies=3)
    tx = await basket(client, {HOT: 2})

    r = await complete(client, tx)

    assert r.status_code == 200
    assert await stock_of(HOT) == 98
    assert len(stack.bus.events(ev.INVENTORY_STOCK)) == 4, "one reply, delivered four times"
    await assert_consistent()


async def test_k_a_reserve_that_arrives_after_the_attempt_was_abandoned_is_refused(client, stack):
    tx = await basket(client, {HOT: 2})
    stack.bus.delay(RESERVATION_REQUESTS, 2.0, times=3)  # all three copies arrive long after the deadline

    r = await complete(client, tx)

    assert r.status_code == 503
    assert await adjustment_states(tx) == [(1, "RELEASED")], "the release got there first"
    await wait_until(
        lambda: sum(e.type == ev.RESERVATION_REFUSED for e in stack.bus.events(ev.INVENTORY_STOCK)) == 3,
        timeout=6.0,
        what="the three late requests being refused",
    )
    assert await stock_of(HOT) == 100
    assert await status_of(client, tx) == "OPEN"
    await assert_consistent()


async def test_a_basket_spanning_several_skus_is_all_or_nothing(client, session):
    await set_stock(session, "SKU-000002", 1)
    tx = await basket(client, {HOT: 2, "SKU-000002": 2})

    r = await complete(client, tx)

    assert r.status_code == 409
    assert await stock_of(HOT) == 100, "the SKU that had enough was not touched either"
    await assert_consistent()


def stock_of_dead_letters(stack) -> int:
    return len(stack.bus.records(ev.DEAD_LETTERS))
