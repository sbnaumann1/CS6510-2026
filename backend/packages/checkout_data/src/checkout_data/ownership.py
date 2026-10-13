"""Table ownership registry — the source of truth for the boundary check.

Every table has exactly one owning (writing) service and an explicit set of
reader services. `scripts/check_boundaries.py` (rule 5) verifies each SQL string
in `checkout_data/repos/<service>.py` against this registry. Must match
specs/005-service-architecture/data-model.md §1.

The seed and invariant CLIs (`checkout_data.seed`, `checkout_data.verify_invariant`)
are operator tools that own the schema itself; they are exempt.
"""

from __future__ import annotations

from dataclasses import dataclass

SERVICES = ("catalog", "checkout", "inventory", "analytics")


@dataclass(frozen=True)
class TableOwnership:
    owner: str
    readers: frozenset[str]


TABLES: dict[str, TableOwnership] = {
    # Written only by the seed tool; immutable during a run.
    "catalog_item": TableOwnership(
        "catalog", frozenset({"catalog", "checkout", "inventory", "analytics"})
    ),
    "inventory_stock": TableOwnership("checkout", frozenset({"checkout", "inventory"})),
    "transaction": TableOwnership("checkout", frozenset({"checkout"})),
    # transaction_item.id is the global scan sequence Analytics tails.
    "transaction_item": TableOwnership("checkout", frozenset({"checkout", "analytics"})),
    "low_stock_alert": TableOwnership("checkout", frozenset({"inventory"})),
    "popular_window_snapshot": TableOwnership("analytics", frozenset({"analytics"})),
}
