"""Checkout's hot-path database access: a bare asyncpg pool (spec 009 scan fast path).

Start and scan are each one atomic statement. Through SQLAlchemy every call builds an
AsyncSession, crosses the async-to-sync bridge to execute, and again to commit and close.
Here the statement is prepared once per connection and run directly: no session, no bridge, no
ORM Row. The statements are the same as repo.py's, with positional parameters.

The pool belongs to one event loop (a connection cannot move between loops), so it is created
lazily on the loop that first needs it and replaced if the loop changes, as it does between
tests. `warm()` opens every connection before traffic; `close()` is for shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
from sqlalchemy.engine import make_url

from checkout_service.config import settings

_pool: asyncpg.Pool | None = None
_loop: asyncio.AbstractEventLoop | None = None
_creating: asyncio.Future | None = None

# Same statements as repo.py `_INSERT_TX` and `_SCAN`. Every parameter is cast once and the same
# way wherever it appears, so PostgreSQL infers one type per parameter.
START = (
    "INSERT INTO transaction (station_id) VALUES ($1::text)"
    " RETURNING id, status::text AS status, item_count, running_total_cents, started_at"
)
SCAN = """
WITH tx AS (
    UPDATE transaction
        SET item_count = item_count + 1,
            running_total_cents = running_total_cents + $2::int
    WHERE id = $1::bigint AND status = 'OPEN'
    RETURNING id, item_count, running_total_cents
), ins AS (
    INSERT INTO transaction_item (transaction_id, sku, unit_price_cents)
    SELECT id, $3::text, $2::int FROM tx
    RETURNING id
)
SELECT tx.item_count, tx.running_total_cents, (SELECT id FROM ins) AS scan_seq
    FROM tx
"""
STATUS = "SELECT status::text AS status FROM transaction WHERE id = $1::bigint"


def _connect_kwargs() -> dict[str, Any]:
    url = make_url(settings.database_url)
    host = url.host
    if not host:
        q = url.query.get("host")
        host = q[0] if isinstance(q, (list, tuple)) else q
    kwargs: dict[str, Any] = {"database": url.database}
    if host:
        kwargs["host"] = host
    if url.port:
        kwargs["port"] = url.port
    if url.username:
        kwargs["user"] = url.username
    if url.password:
        kwargs["password"] = url.password
    return kwargs


async def _create(n: int) -> asyncpg.Pool:
    return await asyncpg.create_pool(min_size=n, max_size=n, command_timeout=30, **_connect_kwargs())


async def get_pool() -> asyncpg.Pool:
    global _pool, _loop, _creating
    loop = asyncio.get_running_loop()
    if _pool is not None and _loop is loop:
        return _pool
    if _creating is not None and _loop is loop:
        return await asyncio.shield(_creating)
    _pool, _loop = None, loop  # first use, or a new loop: forget the old pool
    n = max(1, settings.fast_pool_size)
    _creating = loop.create_task(_create(n))
    try:
        _pool = await asyncio.shield(_creating)
    finally:
        _creating = None
    return _pool


async def warm() -> None:
    await get_pool()


async def close() -> None:
    global _pool, _loop
    pool, _pool, _loop = _pool, None, None
    if pool is not None:
        await pool.close()


async def fetchrow(query: str, *args: Any):
    return await (await get_pool()).fetchrow(query, *args)
