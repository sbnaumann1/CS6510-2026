"""Checkout settings. Reads CHECKOUT_DATABASE_URL first, then DATABASE_URL."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def _database_url() -> str:
    return (
        os.getenv("CHECKOUT_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql+asyncpg:///checkout_checkout?host=/tmp"
    )


@dataclass(frozen=True)
class Settings:
    kafka_bootstrap: str = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
    database_url: str = _database_url()
    catalog_url: str = os.getenv("CATALOG_URL", "http://catalog:8000")
    inventory_url: str = os.getenv("INVENTORY_URL", "http://inventory:8000")
    tx_abandon_minutes: int = _int("TX_ABANDON_MINUTES", 5)
    # Completion saga (contracts/completion-saga.md): a stuck completion becomes eligible
    # for recovery after the lease, and the whole synchronous reserve gets one budget.
    recovery_lease_s: float = _float("RECOVERY_LEASE_S", 10.0)
    recovery_interval_s: float = _float("RECOVERY_INTERVAL_S", 2.0)
    reserve_budget_s: float = _float("RESERVE_BUDGET_S", 4.0)
    # How long a release request waits for Inventory's confirmation (completion-flow.md).
    release_wait_s: float = _float("RELEASE_WAIT_S", 2.0)
    # Scan relay (research R8): a missing scan id is a permanent gap only after this grace.
    relay_gap_grace_ms: int = _int("RELAY_GAP_GRACE_MS", 1000)
    # Keep workers x pool below this database's max_connections (compose-topology.md).
    db_pool_size: int = _int("DB_POOL_SIZE", 10)
    workers: int = _int("WORKERS", 6)

    @property
    def sync_database_url(self) -> str:
        """psycopg2 form of database_url, for the seed CLI."""
        return self.database_url.replace("postgresql+asyncpg", "postgresql+psycopg2")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
