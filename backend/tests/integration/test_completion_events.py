"""The completion flow on events (specs/008-event-driven-kafka/contracts/completion-flow.md):
the shopper-visible results are the contract's, and every step is visible on the topics."""

from __future__ import annotations

from sqlalchemy import text

from checkout_web import events as ev
from tests.failure._helpers import (
    adjustment_states,
    assert_consistent,
    basket,
    complete,
    completion_state,
    set_stock,
    status_of,
    stock_of,
)


async def test_a_completed_basket_has_a_receipt_matching_the_scanned_prices(client, stack):
    items = {i["sku"]: i for i in (await client.get("/items")).json()["items"]}
    tx = await basket(client, {"SKU-000001": 2, "SKU-000002": 1})

    r = await complete(client, tx)

    assert r.status_code == 200
    body = r.json()
    expected = 2 * items["SKU-000001"]["price"] + items["SKU-000002"]["price"]
    assert round(body["totalAmount"], 2) == round(expected, 2)
    assert body["itemCount"] == 3
    assert {line["sku"]: line["quantity"] for line in body["lines"]} == {"SKU-000001": 2, "SKU-000002": 1}
    assert await stock_of("SKU-000001") == 98 and await stock_of("SKU-000002") == 99
    assert await status_of(client, tx) == "COMPLETED"
    await assert_consistent()


async def test_the_flow_is_one_request_and_one_reply_on_the_topics_keyed_by_transaction(client, stack):
    tx = await basket(client, {"SKU-000003": 1})
    r = await complete(client, tx)
    assert r.status_code == 200

    requests = [r_ for r_ in stack.bus.records(ev.CHECKOUT_COMPLETIONS)]
    replies = [r_ for r_ in stack.bus.records(ev.INVENTORY_STOCK)]
    assert [r_.event().type for r_ in requests] == [ev.STOCK_RESERVATION_REQUESTED]
    assert [r_.event().type for r_ in replies] == [ev.STOCK_RESERVED]
    assert requests[0].key == tx and replies[0].key == tx
    request, reply = requests[0].event(), replies[0].event()
    assert request.data == {"transactionId": tx, "epoch": 1, "lines": [{"sku": "SKU-000003", "quantity": 1}]}
    assert reply.data["inReplyTo"] == request.event_id and reply.data["epoch"] == 1


async def test_the_request_id_travels_on_both_events(client, stack):
    tx = await basket(client, {"SKU-000004": 1})
    r = await client.post(f"/transactions/{tx}/complete", json={}, headers={"X-Request-ID": "trace-evt"})
    assert r.status_code == 200
    request = stack.bus.records(ev.CHECKOUT_COMPLETIONS)[0]
    reply = stack.bus.records(ev.INVENTORY_STOCK)[0]
    assert request.event().correlation_id == "trace-evt" and request.headers["x-request-id"] == "trace-evt"
    assert reply.event().correlation_id == "trace-evt" and reply.headers["x-request-id"] == "trace-evt"


async def test_too_little_stock_is_the_contracts_409_and_changes_nothing(client, stack, session):
    await set_stock(session, "SKU-000005", 1)
    tx = await basket(client, {"SKU-000005": 2, "SKU-000006": 1})

    r = await complete(client, tx)

    assert r.status_code == 409 and r.json()["error"] == "INSUFFICIENT_STOCK"
    assert "SKU-000005" in r.json()["message"]
    assert await stock_of("SKU-000005") == 1 and await stock_of("SKU-000006") == 100
    assert await status_of(client, tx) == "OPEN", "the basket is open again"
    assert await adjustment_states(tx) == [(1, "REJECTED")]
    assert await completion_state(tx) == (1, "ABORTED")
    reply = stack.bus.events(ev.INVENTORY_STOCK)[0]
    assert reply.type == ev.STOCK_REJECTED and reply.data["shortSkus"] == ["SKU-000005"]
    await assert_consistent()


async def test_a_rejected_basket_can_be_completed_once_stock_is_back(client, stack, session):
    await set_stock(session, "SKU-000007", 1)
    tx = await basket(client, {"SKU-000007": 2})
    assert (await complete(client, tx)).status_code == 409

    await set_stock(session, "SKU-000007", 5)
    r = await complete(client, tx)

    assert r.status_code == 200
    assert await completion_state(tx) == (2, "COMPLETED"), "the retry is the next epoch"
    assert await adjustment_states(tx) == [(1, "REJECTED"), (2, "APPLIED")]
    assert await stock_of("SKU-000007") == 3
    await assert_consistent()


async def test_every_request_gets_exactly_one_reply(client, stack):
    for sku in ("SKU-000008", "SKU-000009", "SKU-000010"):
        assert (await complete(client, await basket(client, {sku: 1}))).status_code == 200
    n_requests = len(stack.bus.records(ev.CHECKOUT_COMPLETIONS))
    n_replies = len(stack.bus.records(ev.INVENTORY_STOCK))
    assert n_requests == n_replies == 3


async def test_outbox_rows_are_marked_published_after_the_fast_path(client, stack):
    from checkout_service.db import SessionLocal as CheckoutSession
    from inventory_service.db import SessionLocal as InventorySession

    assert (await complete(client, await basket(client, {"SKU-000011": 1}))).status_code == 200
    async with CheckoutSession() as c, InventorySession() as i:
        assert await c.scalar(text("SELECT count(*) FROM outbox WHERE published_at IS NULL")) == 0
        assert await i.scalar(text("SELECT count(*) FROM outbox WHERE published_at IS NULL")) == 0
        assert await c.scalar(text("SELECT count(*) FROM outbox")) == 1
        assert await i.scalar(text("SELECT count(*) FROM outbox")) == 1
