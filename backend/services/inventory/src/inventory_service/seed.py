"""Seed Inventory's database (spec 007 research R9).

    python -m inventory_service.seed --if-empty   # compose `seed-inventory` job
    python -m inventory_service.seed --reset      # drop and recreate everything
    python -m inventory_service.seed              # reset run state, keep the schema

Creates the schema and one stock row per SKU `SKU-{i:06d}` for `i` in
`1..CATALOG_SIZE`, each with STOCK_PER_ITEM units. The SKU naming is a shared
convention with the Catalog seed, not shared data: nothing is read from Catalog.

`--if-empty` seeds only a database with no stock rows yet, so compose re-running the
one-shot seed can never wipe a live database.
"""

from __future__ import annotations

import argparse
import sys
import time

from sqlalchemy import create_engine, text

from inventory_service.config import settings
from inventory_service.models import Base


def _stock_count(conn) -> int | None:
    """Rows in inventory_stock, or None if the table does not exist yet."""
    if conn.execute(text("SELECT to_regclass('public.inventory_stock')")).scalar() is None:
        return None
    return conn.execute(text("SELECT count(*) FROM inventory_stock")).scalar()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="drop and recreate the schema")
    mode.add_argument(
        "--if-empty", action="store_true", help="seed only if there are no stock rows yet"
    )
    args = ap.parse_args(argv)

    engine = create_engine(settings.sync_database_url, future=True)
    started = time.perf_counter()

    if args.if_empty:
        with engine.connect() as conn:
            existing = _stock_count(conn)
        if existing:
            print(f"inventory already seeded ({existing} SKUs); skipping")
            return 0

    with engine.begin() as conn:
        if args.reset:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        Base.metadata.create_all(conn)

    n = settings.catalog_size
    stock = settings.stock_per_item
    with engine.begin() as conn:
        # Run state only: reservations and alerts. The stock rows are reset below.
        conn.execute(text("TRUNCATE stock_adjustment, low_stock_alert, outbox RESTART IDENTITY"))
        conn.execute(text("DELETE FROM inventory_stock"))
        conn.execute(
            text(
                "INSERT INTO inventory_stock (sku, current_stock, initial_stock)"
                " SELECT 'SKU-' || lpad(g::text, 6, '0'), :stock, :stock"
                "   FROM generate_series(1, :n) AS g"
            ),
            {"n": n, "stock": stock},
        )

    print(
        f"seeded {n} SKUs with {stock} units each "
        f"in {time.perf_counter() - started:.2f}s"
    )
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
