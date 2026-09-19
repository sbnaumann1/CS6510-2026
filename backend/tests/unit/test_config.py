"""Unit tests: settings defaults (T042).

The defaults are the Configuration table in data-model.md. conftest overrides
CATALOG_SIZE and STOCK_PER_ITEM to keep the per-test reset fast, so those two are
checked against a freshly reloaded module rather than the live instance.
"""

from __future__ import annotations

from app.config import settings


def test_defaults_match_the_data_model_table(monkeypatch):
    for name in (
        "DATABASE_URL",
        "CATALOG_SIZE",
        "STOCK_PER_ITEM",
        "LOW_STOCK_THRESHOLD",
        "POPULAR_WINDOW_SIZE",
        "POPULAR_SLIDE_INTERVAL",
        "TX_ABANDON_MINUTES",
        "DB_POOL_SIZE",
        "WORKERS",
    ):
        monkeypatch.delenv(name, raising=False)

    import importlib

    import app.config as config_module

    importlib.reload(config_module)
    fresh = config_module.Settings()

    assert fresh.database_url == "postgresql+asyncpg:///checkout?host=/tmp"
    assert fresh.catalog_size == 2000
    assert fresh.stock_per_item == 10000
    assert fresh.low_stock_threshold == 50
    assert fresh.popular_window_size == 1000
    assert fresh.popular_slide_interval == 500
    assert fresh.tx_abandon_minutes == 5
    assert fresh.db_pool_size == 12
    assert fresh.workers == 4


def test_contract_window_defaults_are_the_ones_the_spec_names():
    assert settings.popular_window_size == 1000
    assert settings.popular_slide_interval == 500
    assert settings.low_stock_threshold == 50


def test_workers_times_pool_stays_under_max_connections():
    # PostgreSQL is tuned to max_connections = 200 (research R11).
    assert settings.workers * settings.db_pool_size < 200


def test_sync_url_is_the_psycopg2_form():
    assert settings.sync_database_url.startswith("postgresql+psycopg2")
    assert "asyncpg" not in settings.sync_database_url
