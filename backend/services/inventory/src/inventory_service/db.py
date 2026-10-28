"""Inventory's database access: async engine, sessions, health ping."""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from inventory_service.config import settings

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

# Same pool; for single statements that need no BEGIN/COMMIT (marking an outbox row published).
autocommit_engine: AsyncEngine = engine.execution_options(isolation_level="AUTOCOMMIT")

AutocommitSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    autocommit_engine, expire_on_commit=False, autoflush=False
)

_PING = text("SELECT 1")


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with SessionLocal() as session:
        yield session


async def ping() -> None:
    """One round trip to the database; raises if it is unreachable."""
    async with SessionLocal() as session:
        await session.execute(_PING)


async def dispose_engine() -> None:
    await engine.dispose()
