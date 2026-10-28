"""Test-only twin of the gateway, and the operator transport.

Gateway twin (specs/007-microservices-architecture/contracts/gateway-routes.md): routes
each public request to one of the four service apps in-process, with nginx's location
semantics (an exact match wins, then the longest matching prefix, then the default), and
answers 404 for /internal/. tests/unit/test_gateway_routes.py asserts this table equals the
one in gateway/nginx.conf, so the two cannot drift.

Operator transport: `OpsTransport` is the httpx transport the *operator checks* use to read
each service's `/internal/reports/*` (tools/verify_invariant.py), routing by host name
straight to the owning app. It is not a service mesh: since spec 008 the services never call
one another, they only exchange events (tests/conftest.py starts them on one shared
`MemoryBus`, whose fault hooks replace the fault injection the 007 mesh twin had).

Import this module only after the per-service database URLs and CATALOG_FILE are set: the
service apps bind their engines at import time.
"""

from __future__ import annotations

import httpx

ROUTES: list[tuple[str, str, str]] = [
    ("exact", "/items", "catalog"),
    ("prefix", "/transactions", "checkout"),
    ("prefix", "/inventory/", "inventory"),
    ("prefix", "/analytics/", "analytics"),
    ("prefix", "/internal/", "blocked"),
    ("default", "/", "checkout"),
]

NOT_FOUND = b'{"error":"NOT_FOUND","message":"Not found."}'


def route(path: str) -> str:
    """The service that owns `path`, or "blocked"."""
    for kind, loc, svc in ROUTES:
        if kind == "exact" and path == loc:
            return svc
    prefixes = [(loc, svc) for kind, loc, svc in ROUTES if kind == "prefix" and path.startswith(loc)]
    if prefixes:
        return max(prefixes, key=lambda p: len(p[0]))[1]
    return next(svc for kind, _, svc in ROUTES if kind == "default")


def build_apps() -> dict:
    from analytics_service.main import app as analytics_app
    from catalog_service.main import app as catalog_app
    from checkout_service.main import app as checkout_app
    from inventory_service.main import app as inventory_app

    return {
        "catalog": catalog_app,
        "checkout": checkout_app,
        "inventory": inventory_app,
        "analytics": analytics_app,
    }


def build_dispatch_app(apps: dict | None = None):
    """An ASGI app that forwards each public HTTP request to the owning service's app."""
    apps = apps or build_apps()

    async def dispatch(scope, receive, send):
        if scope["type"] != "http":
            return  # lifespans are not run in-process (see conftest.client)
        target = route(scope["path"])
        if target == "blocked":
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": NOT_FOUND})
            return
        await apps[target](scope, receive, send)

    return dispatch


class OpsTransport(httpx.AsyncBaseTransport):
    """Operator reads, in-process: routes `http://<service>:8000/...` to that service's app."""

    def __init__(self, apps: dict):
        self.apps = apps
        self._asgi = {
            name: httpx.ASGITransport(app=app, raise_app_exceptions=False)
            for name, app in apps.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._asgi[request.url.host].handle_async_request(request)
