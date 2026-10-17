"""Shared data-access library — the only place the services are coupled.

Async engine, session factories, the advisory-lock helper (001 research R1, R6) and
a health ping. The autocommit session factory is only for routes whose database work
is one atomic statement: it skips the BEGIN/COMMIT round trips (spec 006 research R2). Repository functions live in `checkout_data.repos.<service>`, one
module per consuming service; table ownership is declared in
`checkout_data.ownership` (005 research R2, R12).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from checkout_data.config import settings

# Advisory-lock key held by the Analytics leader for the lifetime of its
# persistent popular-items pipeline (005 research R5); arbitrary but stable.
POPULAR_RECOMPUTE_LOCK = 0x5C40_0001

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=0,
    pool_pre_ping=False,
    future=True,
    query_cache_size=1200,
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, autoflush=False
)

# Same pool as `engine`; SQLAlchemy restores the default isolation level when a
# connection is returned, so transactional sessions on that pool stay transactional.
autocommit_engine: AsyncEngine = engine.execution_options(isolation_level="AUTOCOMMIT")

AutocommitSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    autocommit_engine, expire_on_commit=False, autoflush=False
)

_PING = text("SELECT 1")


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — one session per request."""
    async with SessionLocal() as session:
        yield session


async def get_autocommit_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for single-statement routes: each statement commits on its own."""
    async with AutocommitSessionLocal() as session:
        yield session


_TRY_LOCK = text("SELECT pg_try_advisory_lock(:k)")
_UNLOCK = text("SELECT pg_advisory_unlock(:k)")


async def try_advisory_lock(conn: AsyncConnection, key: int) -> bool:
    """Try to take a session-level advisory lock on `conn`; False if held elsewhere.

    Session-level: it outlives the current transaction and is released by
    `advisory_unlock` or when the connection closes.
    """
    return bool(await conn.scalar(_TRY_LOCK, {"k": key}))


async def advisory_unlock(conn: AsyncConnection, key: int) -> None:
    await conn.execute(_UNLOCK, {"k": key})


@asynccontextmanager
async def advisory_lock(conn: AsyncConnection, key: int) -> AsyncIterator[bool]:
    """Try to take a session-level advisory lock; yields False if held elsewhere."""
    got = await try_advisory_lock(conn, key)
    try:
        yield got
    finally:
        if got:
            await advisory_unlock(conn, key)


async def ping() -> None:
    """One round trip to the database; raises if it is unreachable."""
    async with SessionLocal() as session:
        await session.execute(_PING)


async def dispose_engine() -> None:
    await engine.dispose()
