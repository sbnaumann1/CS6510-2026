"""Unit tests: each service's settings defaults (T042).

The defaults are the Configuration table in data-model.md and the per-service environment
in contracts/compose-topology.md. conftest overrides CATALOG_SIZE, STOCK_PER_ITEM and the
database URLs to keep the per-test reset fast, so the defaults are checked against freshly
reloaded modules rather than the live instances.
"""

from __future__ import annotations

import importlib

from analytics_service.config import settings as analytics_settings
from checkout_service.config import settings as checkout_settings
from inventory_service.config import settings as inventory_settings

_ENV = (
    "CHECKOUT_DATABASE_URL",
    "INVENTORY_DATABASE_URL",
    "DATABASE_URL",
    "CATALOG_SIZE",
    "STOCK_PER_ITEM",
    "LOW_STOCK_THRESHOLD",
    "POPULAR_WINDOW_SIZE",
    "POPULAR_SLIDE_INTERVAL",
    "POPULAR_POLL_MS",
    "TX_ABANDON_MINUTES",
    "DB_POOL_SIZE",
    "WORKERS",
    "CATALOG_FILE",
    "KAFKA_BOOTSTRAP",
    "RESERVE_BUDGET_S",
    "RELEASE_WAIT_S",
    "RELAY_GAP_GRACE_MS",
    "SCAN_POLL_MS",
)


def _fresh(monkeypatch, module_name: str):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    module = importlib.import_module(module_name)
    importlib.reload(module)
    return module.Settings()


def test_inventory_defaults_match_the_data_model_table(monkeypatch):
    fresh = _fresh(monkeypatch, "inventory_service.config")
    assert fresh.database_url == "postgresql+asyncpg:///checkout_inventory?host=/tmp"
    assert fresh.catalog_size == 2000
    assert fresh.stock_per_item == 10000
    assert fresh.low_stock_threshold == 50
    assert fresh.db_pool_size == 8
    assert fresh.workers == 2


def test_checkout_defaults_match_the_data_model_table(monkeypatch):
    fresh = _fresh(monkeypatch, "checkout_service.config")
    assert fresh.database_url == "postgresql+asyncpg:///checkout_checkout?host=/tmp"
    assert fresh.tx_abandon_minutes == 5
    assert fresh.recovery_lease_s == 10
    assert fresh.reserve_budget_s == 3
    assert fresh.release_wait_s == 1.5
    assert fresh.relay_gap_grace_ms == 1000
    assert fresh.kafka_bootstrap == "kafka:9092"
    assert fresh.db_pool_size == 10
    assert fresh.workers == 6


def test_analytics_defaults_have_no_database_and_one_worker(monkeypatch):
    fresh = _fresh(monkeypatch, "analytics_service.config")
    assert fresh.popular_window_size == 1000
    assert fresh.popular_slide_interval == 500
    assert fresh.popular_poll_ms == 100
    assert fresh.workers == 1
    assert not hasattr(fresh, "database_url")


def test_catalog_defaults_are_a_file_not_a_database(monkeypatch):
    fresh = _fresh(monkeypatch, "catalog_service.config")
    assert fresh.catalog_file == "/data/catalog.csv"
    assert fresh.catalog_size == 2000
    assert not hasattr(fresh, "database_url")


def test_contract_window_defaults_are_the_ones_the_spec_names():
    assert analytics_settings.popular_window_size == 1000
    assert analytics_settings.popular_slide_interval == 500
    assert inventory_settings.low_stock_threshold == 50


def test_workers_times_pool_stays_under_each_databases_max_connections():
    # compose-topology.md: checkout max_connections 100, inventory 60.
    assert checkout_settings.workers * checkout_settings.db_pool_size < 100
    assert inventory_settings.workers * inventory_settings.db_pool_size < 60


def test_sync_url_is_the_psycopg2_form():
    for settings in (checkout_settings, inventory_settings):
        assert settings.sync_database_url.startswith("postgresql+psycopg2")
        assert "asyncpg" not in settings.sync_database_url
