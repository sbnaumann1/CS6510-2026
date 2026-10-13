"""Environment-backed settings, shared by every service.

Defaults mirror the Configuration table in specs/001-checkout-backend/data-model.md;
the per-service values used in the compose stack are in
specs/005-service-architecture/data-model.md §4.
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
    # Unix-domain socket by default (001 research R3) for host-side tests and
    # scripts. The compose stack overrides this with a TCP URL (005 research R8).
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg:///checkout?host=/tmp"
    )
    catalog_size: int = _int("CATALOG_SIZE", 2000)
    stock_per_item: int = _int("STOCK_PER_ITEM", 10000)
    low_stock_threshold: int = _int("LOW_STOCK_THRESHOLD", 50)
    popular_window_size: int = _int("POPULAR_WINDOW_SIZE", 1000)
    popular_slide_interval: int = _int("POPULAR_SLIDE_INTERVAL", 500)
    # Persistent analytics pipeline (005 research R5-R6): idle sleep of the scan
    # tail, and how long a sequence hole may stay open before it is skipped.
    popular_poll_ms: int = _int("POPULAR_POLL_MS", 100)
    popular_gap_grace_ms: int = _int("POPULAR_GAP_GRACE_MS", 1000)
    tx_abandon_minutes: int = _int("TX_ABANDON_MINUTES", 5)
    # Keep the sum of workers x pool across all services below PostgreSQL
    # max_connections (200; 005 research R8).
    db_pool_size: int = _int("DB_POOL_SIZE", 12)
    workers: int = _int("WORKERS", 4)
    service_name: str = os.getenv("SERVICE_NAME", "dev")

    @property
    def sync_database_url(self) -> str:
        """psycopg2 form of database_url, for the seed/verify CLIs."""
        return self.database_url.replace("postgresql+asyncpg", "postgresql+psycopg2")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
