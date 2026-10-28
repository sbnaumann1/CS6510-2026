"""One request id traces a request across the services it touches (FR-020, SC-008)."""

from __future__ import annotations

import json
import logging

from tests.failure._helpers import RESERVATION_REQUESTS, basket


def step_lines(caplog) -> list[dict]:
    out = []
    for record in caplog.records:
        if record.name == "checkout.steps":
            out.append(json.loads(record.getMessage()))
    return out


async def test_the_id_appears_in_checkout_and_inventorys_logs(client, caplog):
    caplog.set_level(logging.INFO, logger="checkout.steps")
    tx = await basket(client, {"SKU-000001": 2})
    caplog.clear()

    r = await client.post(
        f"/transactions/{tx}/complete", json={}, headers={"X-Request-ID": "trace-1"}
    )

    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-1"
    mine = [line for line in step_lines(caplog) if line["request_id"] == "trace-1"]
    steps = [line["step"] for line in mine]
    assert steps.count("reserve") == 2, "one line from Checkout's saga, one from Inventory"
    assert "begin" in steps and "finalize" in steps
    # The id travelled in the event: Inventory's handler logged under the same request id.
    assert all(line["tx"] == tx for line in mine)
    assert all(line["outcome"] for line in mine)


async def test_a_request_without_an_id_gets_one_and_it_is_echoed(client):
    r = await client.get("/items")
    assert r.headers["x-request-id"]
    other = await client.get("/items")
    assert other.headers["x-request-id"] != r.headers["x-request-id"]


async def test_the_id_follows_a_rollback_too(client, stack, caplog):
    caplog.set_level(logging.INFO, logger="checkout.steps")
    tx = await basket(client, {"SKU-000001": 1})
    caplog.clear()
    stack.bus.drop(RESERVATION_REQUESTS, times=99)  # Inventory never hears the request

    r = await client.post(
        f"/transactions/{tx}/complete", json={}, headers={"X-Request-ID": "trace-2"}
    )

    assert r.status_code == 503
    mine = [line for line in step_lines(caplog) if line["request_id"] == "trace-2"]
    outcomes = {(line["step"], line["outcome"]) for line in mine}
    assert ("reserve", "timeout") in outcomes
    assert ("release", "compensated") in outcomes
