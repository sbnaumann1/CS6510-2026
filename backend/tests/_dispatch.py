"""Test-only twin of the nginx gateway (specs/005-service-architecture/contracts/gateway-routes.md).

Routes each request to one of the four service apps in-process, with nginx's
location semantics: an exact match wins, then the longest matching prefix, then
the default. tests/unit/test_gateway_routes.py asserts this table equals the
one in gateway/nginx.conf, so the two cannot drift.

Import this module only after DATABASE_URL is set: the service apps bind the
shared engine at import time.
"""

from __future__ import annotations

ROUTES: list[tuple[str, str, str]] = [
    ("exact", "/items", "catalog"),
    ("prefix", "/transactions", "checkout"),
    ("prefix", "/inventory/", "inventory"),
    ("prefix", "/analytics/", "analytics"),
    ("default", "/", "checkout"),
]


def route(path: str) -> str:
    """The service that owns `path`."""
    for kind, loc, svc in ROUTES:
        if kind == "exact" and path == loc:
            return svc
    prefixes = [(loc, svc) for kind, loc, svc in ROUTES if kind == "prefix" and path.startswith(loc)]
    if prefixes:
        return max(prefixes, key=lambda p: len(p[0]))[1]
    return next(svc for kind, _, svc in ROUTES if kind == "default")


def build_dispatch_app():
    """An ASGI app that forwards each HTTP request to the owning service's app."""
    from analytics_service.main import app as analytics_app
    from catalog_service.main import app as catalog_app
    from checkout_service.main import app as checkout_app
    from inventory_service.main import app as inventory_app

    apps = {
        "catalog": catalog_app,
        "checkout": checkout_app,
        "inventory": inventory_app,
        "analytics": analytics_app,
    }

    async def dispatch(scope, receive, send):
        if scope["type"] != "http":
            return  # lifespans are not run in-process (see conftest.client)
        await apps[route(scope["path"])](scope, receive, send)

    return dispatch
