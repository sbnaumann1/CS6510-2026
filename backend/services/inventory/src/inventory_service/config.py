"""Inventory settings. Reads INVENTORY_DATABASE_URL first, then DATABASE_URL."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


def _database_url() -> str:
    return (
        os.getenv("INVENTORY_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql+asyncpg:///checkout_inventory?host=/tmp"
    )


@dataclass(frozen=True)
class Settings:
    database_url: str = _database_url()
    catalog_url: str = os.getenv("CATALOG_URL", "http://catalog:8000")
    # Seeding: the deterministic SKU convention (research R9); no data is read from Catalog.
    catalog_size: int = _int("CATALOG_SIZE", 2000)
    stock_per_item: int = _int("STOCK_PER_ITEM", 10000)
    low_stock_threshold: int = _int("LOW_STOCK_THRESHOLD", 50)
    db_pool_size: int = _int("DB_POOL_SIZE", 8)
    workers: int = _int("WORKERS", 2)

    @property
    def sync_database_url(self) -> str:
        """psycopg2 form of database_url, for the seed CLI."""
        return self.database_url.replace("postgresql+asyncpg", "postgresql+psycopg2")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
