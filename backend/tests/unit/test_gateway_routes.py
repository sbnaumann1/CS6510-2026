"""The gateway's nginx route table and the test dispatcher's must be identical.

The in-process suite runs through tests/_dispatch.py; gateway mode and the load
client run through gateway/nginx.conf. If the two drifted, a green in-process
run would prove nothing about the deployed routing.
"""

from __future__ import annotations

import pathlib
import re

from tests._dispatch import ROUTES, route

NGINX_CONF = pathlib.Path(__file__).resolve().parents[2] / "gateway" / "nginx.conf"

_LOCATION = re.compile(r"location\s+(=\s*)?(\S+)\s*\{(.*?)\}", re.S)
_PROXY = re.compile(r"proxy_pass\s+http://(\w+);")


def _nginx_routes() -> set[tuple[str, str, str]]:
    routes = set()
    for exact, path, body in _LOCATION.findall(NGINX_CONF.read_text()):
        if path.startswith("@") or path == "/gateway/healthz":
            continue
        upstream = _PROXY.search(body)
        assert upstream, f"location {path} has no proxy_pass"
        kind = "exact" if exact else ("default" if path == "/" else "prefix")
        routes.add((kind, path, upstream.group(1)))
    return routes


def test_nginx_and_dispatcher_route_tables_match():
    assert _nginx_routes() == set(ROUTES)


def test_item_scans_route_to_checkout_not_catalog():
    assert route("/transactions/tx-1/items") == "checkout"
    assert route("/items") == "catalog"


def test_every_contract_path_routes_to_its_owner():
    assert route("/transactions") == "checkout"
    assert route("/transactions/tx-1/complete") == "checkout"
    assert route("/transactions/tx-1") == "checkout"
    assert route("/inventory/low-stock") == "inventory"
    assert route("/analytics/popular-items") == "analytics"


def test_unknown_paths_go_to_checkout():
    assert route("/nope") == "checkout"
    assert route("/") == "checkout"
