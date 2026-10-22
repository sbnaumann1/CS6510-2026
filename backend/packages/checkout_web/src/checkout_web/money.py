"""Money helpers.

All money is stored and summed as integer cents (001 research R4); conversion to the
contract's `number` shape happens once, at the JSON boundary.
"""

from __future__ import annotations


def cents_to_amount(cents: int) -> float:
    """Integer cents -> the contract's `number` money shape."""
    return round(cents / 100.0, 2)
