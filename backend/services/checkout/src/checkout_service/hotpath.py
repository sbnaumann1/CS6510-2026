"""Raw-ASGI handlers for the two hot routes: start and scan (spec 009 scan fast path).

Scans are about 84% of Checkout's requests. Through FastAPI each one pays for the router, the
dependency graph, a `Request` object and the response class. These two POST routes are served
here directly, in front of the FastAPI app, which still answers everything else (completion,
reads, health, every error for an unknown path or method).

Same contract as the routes in api.py: same validation order (body, then field, then id),
same errors from the same exception classes, same status codes and bodies, and the same
`X-Request-ID` handling (RequestIdMiddleware wraps these handlers too). Switch off with
CHECKOUT_FASTPATH=0.
"""

from __future__ import annotations

import logging
from typing import Any

import orjson

from checkout_service import service as svc
from checkout_web.errors import INTERNAL_ERROR, ApiError, UpstreamUnavailable
from checkout_web.request import RequestIdMiddleware, _required_str, parse_json_body

log = logging.getLogger("checkout")

_JSON = [(b"content-type", b"application/json")]


async def _read_body(receive) -> bytes | None:
    """The whole request body, or None if the client went away."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


async def _respond(send, status: int, content: Any) -> None:
    body = orjson.dumps(content)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [*_JSON, (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


async def _handle(scope, receive, send) -> None:
    raw = await _read_body(receive)
    if raw is None:
        return
    path = scope["path"]
    try:
        body = parse_json_body(raw, required=True)
        if path == "/transactions":
            station_id = _required_str(body, "stationId")
            status, content = 201, await svc.fast_start_transaction(station_id)
        else:
            sku = _required_str(body, "sku")
            tx_id = svc.parse_tx_id(path.split("/")[2])
            status, content = 200, await svc.fast_scan_item(tx_id, sku)
    except ApiError as exc:
        status, content = exc.http_status, {"error": exc.code, "message": exc.message}
    except UpstreamUnavailable as exc:
        log.warning("upstream unavailable: %s", exc)
        status, content = 503, {"error": INTERNAL_ERROR[0], "message": f"{exc.peer} service is unavailable."}
    except Exception as exc:  # noqa: BLE001 - same answer as the app's catch-all handler
        log.exception("unhandled error", exc_info=exc)
        status, content = INTERNAL_ERROR[1], {"error": INTERNAL_ERROR[0], "message": "An unexpected error occurred."}
    await _respond(send, status, content)


def _is_hot(scope) -> bool:
    if scope["type"] != "http" or scope["method"] != "POST":
        return False
    path = scope["path"]
    if path == "/transactions":
        return True
    parts = path.split("/")  # "", "transactions", "<id>", "items"
    return len(parts) == 4 and parts[1] == "transactions" and parts[3] == "items" and bool(parts[2])


class HotPath:
    """ASGI wrapper: hot routes here, everything else (and lifespan) to `app`."""

    def __init__(self, app) -> None:
        self.app = app
        self._hot = RequestIdMiddleware(_handle)

    async def __call__(self, scope, receive, send) -> None:
        if _is_hot(scope):
            await self._hot(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def __getattr__(self, name: str):  # state, router, dependency_overrides ... of the wrapped app
        return getattr(self.app, name)
