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
        kind = "exact" if exact else ("default" if path == "/" else "prefix")
        if "return 404" in body:
            routes.add((kind, path, "blocked"))  # /internal/ is never proxied
            continue
        upstream = _PROXY.search(body)
        assert upstream, f"location {path} has no proxy_pass"
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


def test_internal_routes_are_blocked_not_sent_to_checkout():
    """Without an explicit rule the default location would proxy /internal/... to checkout."""
    assert route("/internal/scans") == "blocked"
    assert route("/internal/reservations/tx-1") == "blocked"
    assert route("/internal/reports/stock") == "blocked"


def test_gateway_stamps_every_request_with_an_id():
    conf = NGINX_CONF.read_text()
    http_level = " ".join(conf[conf.index("http {") : conf.index("server {")].split())
    assert http_level.count("proxy_set_header X-Request-ID $request_id_out;") == 1
    assert "map $http_x_request_id $request_id_out" in http_level


def test_unknown_paths_go_to_checkout():
    assert route("/nope") == "checkout"
    assert route("/") == "checkout"


def test_gateway_clears_hop_by_hop_upgrade_headers():
    """Services run httptools, which mishandles the Java client's `Upgrade: h2c`
    (001 research R14), so the gateway must never forward it (spec 006
    contracts/service-runtime.md). Declared once at http level; a location that set
    its own proxy_set_header would stop inheriting these, so none may."""
    conf = NGINX_CONF.read_text()
    http_level = " ".join(conf[conf.index("http {") : conf.index("server {")].split())
    for directive in (
        'proxy_set_header Connection "";',
        'proxy_set_header Upgrade "";',
        'proxy_set_header HTTP2-Settings "";',
    ):
        assert http_level.count(directive) == 1, directive
    for _, path, body in _LOCATION.findall(conf):
        assert "proxy_set_header" not in body, f"location {path} overrides proxy_set_header"
