"""Stopping background tasks reliably.

`asyncio.wait_for` (Python <= 3.11) can swallow a cancellation that races with its inner
future completing; a consumer loop that polls with a timeout can then keep running after
`task.cancel()`, and a plain `await task` waits for ever. `cancel_and_wait` therefore sends
the cancellation again until the task is really done, so shutdown (and every test teardown)
terminates.
"""

from __future__ import annotations

import asyncio
from typing import Iterable


async def cancel_and_wait(tasks: Iterable[asyncio.Task], *, retry_s: float = 0.25) -> None:
    pending = {t for t in tasks if not t.done()}
    while pending:
        for t in pending:
            t.cancel()
        _, pending = await asyncio.wait(pending, timeout=retry_s)
