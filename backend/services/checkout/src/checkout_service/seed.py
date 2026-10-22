"""Seed Checkout's database (spec 007 research R9).

    python -m checkout_service.seed --if-empty   # compose `seed-checkout` job
    python -m checkout_service.seed --reset      # drop and recreate everything
    python -m checkout_service.seed              # clear run state, keep the schema

Checkout owns no reference data: this creates an empty schema. `--if-empty` creates it
only when it is missing, so compose re-running the one-shot seed can never wipe a live
database.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, text

from checkout_service.config import settings
from checkout_service.models import Base


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="drop and recreate the schema")
    mode.add_argument(
        "--if-empty", action="store_true", help="create the schema only if it is missing"
    )
    args = ap.parse_args(argv)

    engine = create_engine(settings.sync_database_url, future=True)
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT to_regclass('public.transaction')")).scalar()
        if args.if_empty and exists is not None:
            print("checkout schema already present; skipping")
            return 0
        if args.reset:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        Base.metadata.create_all(conn)
        if not args.reset and exists is not None:
            # Run state only; the schema stays.
            conn.execute(text("TRUNCATE completion, transaction_item, transaction RESTART IDENTITY CASCADE"))
        # A new scan log: Analytics notices the change and rebuilds instead of trusting a
        # cursor into the old one.
        conn.execute(
            text(
                "INSERT INTO scan_log (id, token) VALUES (1, md5(random()::text || clock_timestamp()::text))"
                " ON CONFLICT (id) DO UPDATE SET token = EXCLUDED.token"
            )
        )
    print("checkout schema ready")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
