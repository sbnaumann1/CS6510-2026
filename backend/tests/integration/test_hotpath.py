"""The raw-ASGI hot path (start and scan) answers exactly as the FastAPI routes do (spec 009).

Each request is sent twice, once to the hot path and once straight to the wrapped FastAPI app,
and the status, the headers that matter and the body must match. Ids differ between two starts
and counts differ between two scans, so successful bodies are compared field by field with
those normalised; every error body is compared byte for byte.
"""

from __future__ import annotations

import httpx
import pytest

from checkout_service import main as checkout_main


def _clients():
    hot = httpx.AsyncClient(transport=httpx.ASGITransport(app=checkout_main.app), base_url="http://t")
    plain = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=checkout_main.app.app), base_url="http://t"
    )
    return hot, plain


pytestmark = pytest.mark.skipif(
    not hasattr(checkout_main.app, "_hot"), reason="CHECKOUT_FASTPATH=0: there is no hot path to compare"
)


async def _both(method, path_for, **kw):
    """Send to each client; `path_for(which)` lets a test use a different transaction per side."""
    hot, plain = _clients()
    async with hot, plain:
        a = await hot.request(method, path_for("hot"), **kw)
        b = await plain.request(method, path_for("plain"), **kw)
    return a, b


def _same_response(a, b):
    assert a.status_code == b.status_code
    assert a.headers["content-type"] == b.headers["content-type"]
    assert "x-request-id" in a.headers and "x-request-id" in b.headers
    assert len(a.headers.get_list("x-request-id")) == 1


async def _open(client) -> str:
    return (await client.post("/transactions", json={"stationId": "s"})).json()["transactionId"]


async def test_start_matches_the_fastapi_route(client):
    a, b = await _both("POST", lambda _: "/transactions", json={"stationId": "station-9"})
    _same_response(a, b)
    assert a.status_code == 201
    ja, jb = a.json(), b.json()
    assert ja.keys() == jb.keys()
    for field in ("stationId", "status", "itemCount", "runningTotal"):
        assert ja[field] == jb[field]
    assert ja["startedAt"].endswith("+00:00") and jb["startedAt"].endswith("+00:00")


async def test_scan_matches_the_fastapi_route(client):
    tx_hot, tx_plain = await _open(client), await _open(client)
    paths = {"hot": f"/transactions/{tx_hot}/items", "plain": f"/transactions/{tx_plain}/items"}
    for sku in ("SKU-000001", "SKU-000001", "SKU-000002"):
        a, b = await _both("POST", lambda w: paths[w], json={"sku": sku})
        _same_response(a, b)
        ja, jb = a.json(), b.json()
        assert a.status_code == 200 and ja.keys() == jb.keys()
        for field in ("sku", "name", "unitPrice", "itemCount", "runningTotal"):
            assert ja[field] == jb[field], field


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": b""},                                   # missing body
        {"content": b"{not json"},                          # malformed JSON
        {"content": b"[1, 2]"},                             # JSON but not an object
        {"json": {}},                                       # missing field
        {"json": {"sku": ""}},                              # empty string
        {"json": {"sku": 7}},                               # wrong type
        {"json": {"sku": "NO-SUCH-SKU"}},                   # unknown SKU
    ],
)
async def test_scan_errors_are_byte_for_byte_the_same(client, kwargs):
    tx = await _open(client)
    a, b = await _both("POST", lambda _: f"/transactions/{tx}/items", **kwargs)
    _same_response(a, b)
    assert a.content == b.content


@pytest.mark.parametrize("bad_id", ["tx-999999999", "nonsense", "tx-", "tx-1x", "TX-1"])
async def test_scan_unknown_or_malformed_transaction_is_the_same(client, bad_id):
    a, b = await _both("POST", lambda _: f"/transactions/{bad_id}/items", json={"sku": "SKU-000001"})
    _same_response(a, b)
    assert a.content == b.content


async def test_scan_on_a_completed_transaction_is_the_same_409(client):
    tx = await _open(client)
    await client.post(f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
    assert (await client.post(f"/transactions/{tx}/complete", json={})).status_code == 200
    a, b = await _both("POST", lambda _: f"/transactions/{tx}/items", json={"sku": "SKU-000001"})
    _same_response(a, b)
    assert a.status_code == 409 and a.content == b.content


@pytest.mark.parametrize("kwargs", [{"content": b""}, {"content": b"{x"}, {"json": {}}, {"json": {"stationId": ""}}])
async def test_start_errors_are_byte_for_byte_the_same(client, kwargs):
    a, b = await _both("POST", lambda _: "/transactions", **kwargs)
    _same_response(a, b)
    assert a.content == b.content


async def test_other_routes_and_methods_still_reach_fastapi(client):
    tx = await _open(client)
    assert (await client.get(f"/transactions/{tx}")).status_code == 200
    assert (await client.get(f"/transactions/{tx}/items")).status_code == 405
    assert (await client.post(f"/transactions/{tx}/items/extra", json={})).status_code == 404
