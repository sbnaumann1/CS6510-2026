"""HTTP client for service-to-service calls (contracts/internal-apis.md "Client behavior").

One pooled `httpx.AsyncClient` per peer, keep-alive, short connect timeout, and
retries only for calls the caller marks idempotent. Forwards `X-Request-ID`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from checkout_web.errors import UpstreamUnavailable
from checkout_web.request import REQUEST_ID_HEADER, current_request_id, log_step, new_request_id

CONNECT_TIMEOUT_S = 0.5
READ_TIMEOUT_S = 2.0
_BACKOFF_S = 0.05


class ServiceClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        peer: str | None = None,
    ) -> None:
        self.peer = peer or urlparse(base_url).hostname or base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            limits=httpx.Limits(max_keepalive_connections=64, max_connections=256),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        retries: int = 0,
        idempotent: bool = False,
        budget_s: float | None = None,
    ) -> httpx.Response:
        """Send one request. HTTP statuses below 500 are returned to the caller (a 409
        is an answer, not a failure). Connection errors, timeouts and 5xx are retried
        only when `idempotent`, up to `retries` extra attempts and `budget_s` in total;
        when they run out, `UpstreamUnavailable` is raised."""
        attempts = 1 + (retries if idempotent else 0)
        started = time.monotonic()
        headers = {REQUEST_ID_HEADER: current_request_id() or new_request_id()}
        read_timeout = READ_TIMEOUT_S if timeout is None else timeout
        t = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_S)
        last = ""
        for attempt in range(attempts):
            t0 = time.monotonic()
            try:
                response = await self._client.request(
                    method, path, json=json, params=params, headers=headers, timeout=t
                )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}"
                outcome = "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
            else:
                if response.status_code < 500:
                    return response
                last = f"HTTP {response.status_code}"
                outcome = "error"
            log_step(
                f"call {self.peer}",
                outcome=outcome,
                elapsed_ms=(time.monotonic() - t0) * 1000,
                path=path,
                attempt=attempt + 1,
            )
            if attempt + 1 >= attempts:
                break
            if budget_s is not None and time.monotonic() - started + _BACKOFF_S >= budget_s:
                break
            await asyncio.sleep(_BACKOFF_S * (attempt + 1))
        raise UpstreamUnavailable(self.peer, last)
