"""Async engine, session factory, and the advisory-lock helper (research R1, R6)."""

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

from app.config import settings

# Advisory-lock key for the popular-window recompute; arbitrary but stable.
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


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — one session per request."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def advisory_lock(conn: AsyncConnection, key: int) -> AsyncIterator[bool]:
    """Try to take a session-level advisory lock; yields False if held elsewhere.

    Used so exactly one worker recomputes the popular-items window per slide.
    """
    got = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
    try:
        yield bool(got)
    finally:
        if got:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def dispose_engine() -> None:
    await engine.dispose()
