"""Money helpers.

All money is stored and summed as integer cents (research R4); conversion to the
contract's `number` shape happens once, at the JSON boundary.
"""

from __future__ import annotations

import math


# Catalog price formula, byte-compatible with mockserver/MockServer.java:
#     double price = 0.5 + (i % 47) * 0.35;
#     Math.round(price * 100.0) / 100.0
# Java's Math.round(double) is floor(x + 0.5), NOT Python's banker's rounding,
# so it is spelled out here rather than using round().
def catalog_price_cents(index: int) -> int:
    """Price in cents for 1-based catalog index `index`."""
    price = 0.5 + (index % 47) * 0.35
    return math.floor(price * 100.0 + 0.5)


def cents_to_amount(cents: int) -> float:
    """Integer cents -> the contract's `number` money shape."""
    return round(cents / 100.0, 2)
