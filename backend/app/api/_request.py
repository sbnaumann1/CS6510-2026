"""Request-parsing helpers shared by the API routers.

Validation is done by hand rather than through Pydantic on the hot paths
(research R7 of 001), and error bodies follow error-catalog.md.
"""

from __future__ import annotations

from typing import Any

import orjson
from fastapi import Request

from app.errors import INVALID_REQUEST, ApiError


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
