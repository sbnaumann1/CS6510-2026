"""Environment-backed settings.

Defaults mirror the Configuration table in specs/001-checkout-backend/data-model.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # Unix-domain socket by default (research R3): no TCP hop on the hot path.
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg:///checkout?host=/tmp"
    )
    catalog_size: int = _int("CATALOG_SIZE", 2000)
    stock_per_item: int = _int("STOCK_PER_ITEM", 10000)
    low_stock_threshold: int = _int("LOW_STOCK_THRESHOLD", 50)
    popular_window_size: int = _int("POPULAR_WINDOW_SIZE", 1000)
    popular_slide_interval: int = _int("POPULAR_SLIDE_INTERVAL", 500)
    tx_abandon_minutes: int = _int("TX_ABANDON_MINUTES", 5)
    # Keep workers x pool below PostgreSQL max_connections (200 after R11 tuning).
    db_pool_size: int = _int("DB_POOL_SIZE", 12)
    workers: int = _int("WORKERS", 4)

    @property
    def sync_database_url(self) -> str:
        """psycopg2 form of database_url, for the seed/verify scripts."""
        return self.database_url.replace("postgresql+asyncpg", "postgresql+psycopg2")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
