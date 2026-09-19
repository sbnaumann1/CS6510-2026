"""Error contract.

Every error body is the contract's ApiError shape — exactly two string fields,
never any other top-level key. See specs/001-checkout-backend/contracts/error-catalog.md.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("checkout")

# code -> HTTP status
INVALID_REQUEST = ("INVALID_REQUEST", 400)
TRANSACTION_NOT_FOUND = ("TRANSACTION_NOT_FOUND", 404)
SKU_NOT_FOUND = ("SKU_NOT_FOUND", 404)
TRANSACTION_NOT_OPEN = ("TRANSACTION_NOT_OPEN", 409)
EMPTY_BASKET = ("EMPTY_BASKET", 409)
INSUFFICIENT_STOCK = ("INSUFFICIENT_STOCK", 409)
INTERNAL_ERROR = ("INTERNAL_ERROR", 500)


class ApiError(Exception):
    """Raised anywhere in the stack; rendered as the ApiError body."""

    def __init__(self, code_status: tuple[str, int], message: str) -> None:
        self.code, self.http_status = code_status
        self.message = message
        super().__init__(message)


def _body(code: str, message: str, status: int) -> ORJSONResponse:
    return ORJSONResponse(status_code=status, content={"error": code, "message": message})


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> ORJSONResponse:
        return _body(exc.code, exc.message, exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> ORJSONResponse:
        # FastAPI would answer 422 {"detail": [...]}; the contract requires
        # 400 {"error","message"}.
        detail = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in detail.get("loc", ()) if p != "body")
        msg = detail.get("msg", "Request body is not valid.")
        return _body(
            INVALID_REQUEST[0],
            f"{loc}: {msg}" if loc else msg,
            INVALID_REQUEST[1],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> ORJSONResponse:
        code = {
            400: INVALID_REQUEST[0],
            404: TRANSACTION_NOT_FOUND[0],
            409: TRANSACTION_NOT_OPEN[0],
        }.get(exc.status_code, INTERNAL_ERROR[0])
        return _body(code, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> ORJSONResponse:
        # Traceback goes to the log, never into the response body.
        log.exception("unhandled error", exc_info=exc)
        return _body(
            INTERNAL_ERROR[0], "An unexpected error occurred.", INTERNAL_ERROR[1]
        )
