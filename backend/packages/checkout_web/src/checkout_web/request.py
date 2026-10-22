"""Request-parsing helpers shared by the API routers.

Validation is done by hand rather than through Pydantic on the hot paths
(research R7 of 001), and error bodies follow error-catalog.md.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import orjson
from fastapi import Request

from checkout_web.errors import INVALID_REQUEST, ApiError

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
REQUEST_ID_HEADER = "x-request-id"


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def current_request_id() -> str | None:
    """The id of the request being handled, or None outside a request."""
    return _request_id.get()


def set_request_id(value: str | None):
    """Set the id for background work; returns a token for `_request_id.reset`."""
    return _request_id.set(value)


class RequestIdMiddleware:
    """Reads `X-Request-ID` (or generates one), exposes it to the handler through a
    context variable and echoes it on the response (research R12)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = None
        for name, value in scope["headers"]:
            if name == b"x-request-id":
                rid = value.decode("latin-1")
                break
        rid = rid or new_request_id()
        token = _request_id.set(rid)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)


_steps = logging.getLogger("checkout.steps")
_SERVICE = os.getenv("SERVICE_NAME", "dev")


def _ensure_step_handler() -> None:
    # uvicorn runs with --log-level warning, which would drop these INFO lines, so the
    # step logger owns a handler and level of its own.
    if not _steps.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _steps.addHandler(handler)
    _steps.setLevel(logging.INFO)


def log_step(
    step: str,
    *,
    outcome: str,
    tx: str | None = None,
    elapsed_ms: float | None = None,
    **extra: Any,
) -> None:
    """One JSON log line per coordination step or outbound call (research R12)."""
    _ensure_step_handler()
    record = {
        "request_id": current_request_id(),
        "service": _SERVICE,
        "step": step,
        "tx": tx,
        "outcome": outcome,
        "elapsed_ms": None if elapsed_ms is None else round(elapsed_ms, 1),
        **extra,
    }
    _steps.info(json.dumps(record))


async def _json_body(request: Request, *, required: bool) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        if required:
            raise ApiError(INVALID_REQUEST, "A JSON body is required.")
        return {}
    try:
        body = orjson.loads(raw)
    except Exception:
        raise ApiError(INVALID_REQUEST, "Request body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise ApiError(INVALID_REQUEST, "Request body must be a JSON object.")
    return body


def _required_str(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ApiError(INVALID_REQUEST, f"'{field}' must be a non-empty string.")
    return value


def positive_int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(
            INVALID_REQUEST, f"'{name}' must be a positive integer."
        ) from None
    if value <= 0:
        raise ApiError(INVALID_REQUEST, f"'{name}' must be a positive integer.")
    return value
