"""Deterministic seed / reset (001 research R12).

    python -m checkout_data.seed --if-empty   # compose `seed` step (005 research R7)
    python -m checkout_data.seed --reset      # drop and recreate everything
    python -m checkout_data.seed              # reset run state, keep the schema

Loads CATALOG_SIZE items using the mock server's exact SKU/name/price formulas,
sets stock to STOCK_PER_ITEM, and clears transactions, line items, alerts and the
window snapshot.

`--if-empty` seeds only a database with no catalog yet, so compose re-running the
one-shot seed when a single service is started can never wipe a live database.
A fresh measured run comes from `docker compose down -v` (spec FR-005).
"""

from __future__ import annotations

import argparse
import sys
import time

from sqlalchemy import create_engine, text

from checkout_data.config import settings
from checkout_data.models import Base
from checkout_data.money import catalog_price_cents


def _catalog_count(conn) -> int | None:
    """Rows in catalog_item, or None if the table does not exist yet."""
    if conn.execute(text("SELECT to_regclass('public.catalog_item')")).scalar() is None:
        return None
    return conn.execute(text("SELECT count(*) FROM catalog_item")).scalar()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--reset",
        action="store_true",
        help="drop and recreate the schema before seeding",
    )
    mode.add_argument(
        "--if-empty",
        action="store_true",
        help="seed only if the catalog is absent or empty; otherwise do nothing",
    )
    args = ap.parse_args(argv)

    engine = create_engine(settings.sync_database_url, future=True)
    started = time.perf_counter()

    if args.if_empty:
        with engine.connect() as conn:
            existing = _catalog_count(conn)
        if existing:
            print(f"catalog already seeded ({existing} items); skipping")
            return 0

    with engine.begin() as conn:
        if args.reset:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        Base.metadata.create_all(conn)

    n = settings.catalog_size
    stock = settings.stock_per_item
    rows = [
        {"sku": f"SKU-{i:06d}", "name": f"Item {i}", "cents": catalog_price_cents(i)}
        for i in range(1, n + 1)
    ]

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO catalog_item (sku, name, price_cents)"
                " VALUES (:sku, :name, :cents)"
                " ON CONFLICT (sku) DO UPDATE"
                " SET name = EXCLUDED.name, price_cents = EXCLUDED.price_cents"
            ),
            rows,
        )
        conn.execute(
            text(
                "INSERT INTO inventory_stock (sku, current_stock, initial_stock)"
                " SELECT sku, :stock, :stock FROM catalog_item"
                " ON CONFLICT (sku) DO UPDATE"
                " SET current_stock = EXCLUDED.current_stock,"
                "     initial_stock = EXCLUDED.initial_stock"
            ),
            {"stock": stock},
        )
        # Clear run state so every measured run starts from the same point.
        conn.execute(text("TRUNCATE transaction_item, low_stock_alert RESTART IDENTITY"))
        conn.execute(text("TRUNCATE transaction RESTART IDENTITY CASCADE"))
        conn.execute(text("DELETE FROM popular_window_snapshot"))

    engine.dispose()
    elapsed = time.perf_counter() - started
    print(
        f"seeded {n} catalog items @ {stock} units each "
        f"(threshold {settings.low_stock_threshold}) in {elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
