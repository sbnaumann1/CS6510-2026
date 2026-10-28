"""Analytics settings. There is no database: its state is derived and held in memory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


@dataclass(frozen=True)
class Settings:
    kafka_bootstrap: str = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
    catalog_url: str = os.getenv("CATALOG_URL", "http://catalog:8000")
    checkout_url: str = os.getenv("CHECKOUT_URL", "http://checkout:8000")
    popular_window_size: int = _int("POPULAR_WINDOW_SIZE", 1000)
    popular_slide_interval: int = _int("POPULAR_SLIDE_INTERVAL", 500)
    # Persistent pipeline (005 research R5-R6): idle sleep of the scan tail, and how long
    # a sequence hole may stay open before it is skipped.
    popular_poll_ms: int = _int("POPULAR_POLL_MS", 100)
    popular_gap_grace_ms: int = _int("POPULAR_GAP_GRACE_MS", 1000)
    # Exactly one instance runs. There is no leader election, because nothing shared is
    # written (research R5); a second instance would only duplicate work.
    workers: int = _int("WORKERS", 1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
