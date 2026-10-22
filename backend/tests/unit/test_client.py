"""The service-to-service client (contracts/internal-apis.md "Client behavior")."""

from __future__ import annotations

import httpx
import pytest

from checkout_web.client import ServiceClient
from checkout_web.errors import UpstreamUnavailable
from checkout_web.request import set_request_id


class Script(httpx.AsyncBaseTransport):
    """Answers from a list of outcomes: an int is a status code, an Exception is raised."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"ok": outcome < 400})


def client(*outcomes) -> tuple[ServiceClient, Script]:
    script = Script(*outcomes)
    return ServiceClient("http://peer:8000", transport=script, peer="peer"), script


async def test_success_is_returned_as_is():
    c, script = client(200)
    response = await c.request("GET", "/x")
    assert response.status_code == 200 and len(script.requests) == 1


async def test_client_errors_are_answers_not_failures():
    c, script = client(409)
    response = await c.request("POST", "/x", idempotent=True, retries=2)
    assert response.status_code == 409
    assert len(script.requests) == 1, "a 409 is an answer, not something to retry"


async def test_timeout_then_retry_then_success():
    c, script = client(httpx.ReadTimeout("slow"), 503, 200)
    response = await c.request("POST", "/x", idempotent=True, retries=2)
    assert response.status_code == 200
    assert len(script.requests) == 3


async def test_retries_exhausted_raises_upstream_unavailable():
    c, script = client(httpx.ConnectError("down"), httpx.ConnectError("down"), httpx.ConnectError("down"))
    with pytest.raises(UpstreamUnavailable) as err:
        await c.request("GET", "/x", idempotent=True, retries=2)
    assert err.value.peer == "peer"
    assert len(script.requests) == 3


async def test_a_non_idempotent_call_is_never_retried():
    c, script = client(httpx.ReadTimeout("slow"), 200)
    with pytest.raises(UpstreamUnavailable):
        await c.request("POST", "/x", retries=5)
    assert len(script.requests) == 1


async def test_the_time_budget_stops_retries_early():
    c, script = client(*[503] * 10)
    with pytest.raises(UpstreamUnavailable):
        await c.request("GET", "/x", idempotent=True, retries=9, budget_s=0.0)
    assert len(script.requests) == 1


async def test_request_id_is_forwarded_or_generated():
    c, script = client(200, 200)
    token = set_request_id("trace-me")
    try:
        await c.request("GET", "/x")
    finally:
        set_request_id(None)
        del token
    assert script.requests[0].headers["x-request-id"] == "trace-me"
    await c.request("GET", "/x")
    assert script.requests[1].headers["x-request-id"], "background work gets a generated id"
