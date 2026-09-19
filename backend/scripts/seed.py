#!/usr/bin/env python
"""Deterministic seed / reset (research R12).

    uv run python scripts/seed.py --reset

`--reset` recreates the schema, loads CATALOG_SIZE items using the mock server's
exact SKU/name/price formulas, sets stock to STOCK_PER_ITEM, and clears
transactions, line items, alerts and the window snapshot. It is idempotent, and
must be re-run before every measured load run — a run started against depleted
stock is not comparable to anything.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import Base  # noqa: E402
from app.money import catalog_price_cents  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--reset",
        action="store_true",
        help="drop and recreate the schema before seeding",
    )
    args = ap.parse_args()

    engine = create_engine(settings.sync_database_url, future=True)
    started = time.perf_counter()

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

    elapsed = time.perf_counter() - started
    print(
        f"seeded {n} catalog items @ {stock} units each "
        f"(threshold {settings.low_stock_threshold}) in {elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
