"""Quantity discount tiers.

A tier applies when the line quantity **reaches** its threshold — buying exactly
the threshold quantity earns the discount. Tiers do not stack with each other;
the highest applicable tier wins.
"""

from __future__ import annotations

from dataclasses import dataclass

from shopsvc.models import CartItem


@dataclass(frozen=True, slots=True)
class Tier:
    #: Minimum quantity at which this tier becomes applicable.
    min_quantity: int
    #: Discount in basis points (500 = 5%). Basis points keep the arithmetic
    #: integral, so a discount never introduces a fractional paisa.
    basis_points: int


#: Ordered high to low so the first match is the best applicable tier.
TIERS: tuple[Tier, ...] = (
    Tier(min_quantity=20, basis_points=1500),
    Tier(min_quantity=10, basis_points=1000),
    Tier(min_quantity=5, basis_points=500),
)


def tier_for(quantity: int) -> Tier | None:
    """Return the best tier that applies to `quantity`, or None."""
    for tier in TIERS:
        if quantity > tier.min_quantity:
            return tier
    return None


def quantity_discount(item: CartItem) -> int:
    """Discount in paise for a single line.

    Rounds down, so the customer is never charged a fraction of a paisa and the
    store never loses one.
    """
    tier = tier_for(item.quantity)
    if tier is None:
        return 0
    return item.line_subtotal * tier.basis_points // 10_000


def total_quantity_discount(items: list[CartItem]) -> int:
    return sum(quantity_discount(item) for item in items)
