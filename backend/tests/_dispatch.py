"""Test-only twins of the gateway and of the service mesh.

Gateway twin (specs/007-microservices-architecture/contracts/gateway-routes.md): routes
each public request to one of the four service apps in-process, with nginx's location
semantics (an exact match wins, then the longest matching prefix, then the default), and
answers 404 for /internal/. tests/unit/test_gateway_routes.py asserts this table equals the
one in gateway/nginx.conf, so the two cannot drift.

Mesh twin: `MeshTransport` is the httpx transport the services use to call each other. It
routes by host name (`catalog`, `checkout`, `inventory`, `analytics`) straight to the
owning app, and can drop, delay or fail chosen calls so failure tests are deterministic.

Import this module only after the per-service database URLs and CATALOG_FILE are set: the
service apps bind their engines at import time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

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


@dataclass
class _Fault:
    target: str
    path_prefix: str
    method: str | None
    times: int
    mode: str
    skip: int
    delay: float

    def matches(self, host: str, method: str, path: str) -> bool:
        return (
            host == self.target
            and path.startswith(self.path_prefix)
            and (self.method is None or method == self.method)
        )


class MeshTransport(httpx.AsyncBaseTransport):
    """Service-to-service calls, in-process, with fault injection.

    `fail(target, path_prefix, times=N, mode=..., after=K)` lets the first K matching calls
    through, then applies `mode` to the next N:

      error          raise a connection error; the call never reaches the target
      timeout        raise a read timeout; the call never reaches the target
      http500        answer 500 without calling the target
      drop_response  perform the call, then raise a read timeout (a lost acknowledgement)
      delay          wait `delay` seconds, then perform the call normally
    """

    def __init__(self, apps: dict):
        self._asgi = {
            name: httpx.ASGITransport(app=app, raise_app_exceptions=False)
            for name, app in apps.items()
        }
        self._faults: list[_Fault] = []
        self.calls: list[tuple[str, str, str]] = []

    def fail(
        self,
        target: str,
        path_prefix: str = "/",
        *,
        times: int = 1,
        mode: str = "error",
        after: int = 0,
        method: str | None = None,
        delay: float = 0.0,
    ) -> None:
        self._faults.append(_Fault(target, path_prefix, method, times, mode, after, delay))

    def clear_faults(self) -> None:
        self._faults.clear()

    def calls_to(self, target: str, path_prefix: str = "/", method: str | None = None) -> int:
        return sum(
            1
            for host, m, path in self.calls
            if host == target and path.startswith(path_prefix) and (method is None or m == method)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host, method, path = request.url.host, request.method, request.url.path
        self.calls.append((host, method, path))

        for fault in self._faults:
            if not fault.matches(host, method, path):
                continue
            if fault.skip > 0:
                fault.skip -= 1
                continue
            if fault.times <= 0:
                continue
            fault.times -= 1
            if fault.mode == "error":
                raise httpx.ConnectError("injected connection error", request=request)
            if fault.mode == "timeout":
                raise httpx.ReadTimeout("injected timeout", request=request)
            if fault.mode == "http500":
                return httpx.Response(500, json={"error": "INTERNAL_ERROR", "message": "injected"})
            if fault.mode == "delay":
                await asyncio.sleep(fault.delay)
                break
            if fault.mode == "drop_response":
                response = await self._asgi[host].handle_async_request(request)
                await response.aread()
                raise httpx.ReadTimeout("injected lost response", request=request)
            raise ValueError(f"unknown fault mode {fault.mode!r}")

        return await self._asgi[host].handle_async_request(request)
