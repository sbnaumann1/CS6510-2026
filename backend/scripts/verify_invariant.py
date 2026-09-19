#!/usr/bin/env python
"""The graded correctness check (SC-003, SC-004).

From the course README:

    for every SKU, `initial_stock - final_stock` must equal the total number of
    completed-transaction line items for that SKU, and final stock must never be
    negative.

    uv run python scripts/verify_invariant.py

Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402

QUERY = text(
    """
SELECT s.sku,
       s.initial_stock,
       s.current_stock,
       s.initial_stock - s.current_stock AS decremented,
       coalesce(c.sold, 0)               AS sold
  FROM inventory_stock s
  LEFT JOIN (
        SELECT i.sku, count(*) AS sold
          FROM transaction_item i
          JOIN transaction t ON t.id = i.transaction_id
         WHERE t.status = 'COMPLETED'
         GROUP BY i.sku
  ) c ON c.sku = s.sku
 ORDER BY s.sku
"""
)


def main() -> int:
    engine = create_engine(settings.sync_database_url, future=True)
    with engine.connect() as conn:
        rows = conn.execute(QUERY).all()

    if not rows:
        print("FAIL: inventory_stock is empty — run scripts/seed.py --reset first")
        return 1

    mismatched = [r for r in rows if r.decremented != r.sold]
    negative = [r for r in rows if r.current_stock < 0]
    total_sold = sum(r.sold for r in rows)
    total_decremented = sum(r.decremented for r in rows)

    print(f"SKUs checked:        {len(rows)}")
    print(f"Units decremented:   {total_decremented}")
    print(f"Completed line items:{total_sold:>8}")
    print(f"Negative stock SKUs: {len(negative)}")
    print(f"Mismatched SKUs:     {len(mismatched)}")

    if not mismatched and not negative:
        print("\nPASS — initial_stock - current_stock == completed line items, per SKU")
        return 0

    print("\nFAIL — worst offenders:")
    worst = sorted(mismatched, key=lambda r: abs(r.decremented - r.sold), reverse=True)
    for r in worst[:10]:
        drift = r.decremented - r.sold
        print(
            f"  {r.sku}: initial={r.initial_stock} current={r.current_stock} "
            f"decremented={r.decremented} sold={r.sold} drift={drift:+d}"
        )
    for r in negative[:10]:
        print(f"  {r.sku}: NEGATIVE STOCK {r.current_stock}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
