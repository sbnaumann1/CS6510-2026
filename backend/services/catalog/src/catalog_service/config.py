"""Catalog settings. The catalog is a read-only file; there is no database."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


@dataclass(frozen=True)
class Settings:
    kafka_bootstrap: str = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
    # Written by the seed job, mounted read-only into the service (data-model.md §1).
    catalog_file: str = os.getenv("CATALOG_FILE", "/data/catalog.csv")
    catalog_size: int = _int("CATALOG_SIZE", 2000)  # seed only
    workers: int = _int("WORKERS", 2)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
