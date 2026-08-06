"""Promo codes.

Promotions **stack additively**: applying two 10% codes discounts 20% of the
subtotal, not 19%. The combined discount is capped so a stack of codes can never
discount an order below a floor.
"""

from __future__ import annotations

from dataclasses import dataclass

from shopsvc.errors import InvalidPromotion


@dataclass(frozen=True, slots=True)
class Promotion:
    code: str
    #: Discount in basis points (1000 = 10%).
    basis_points: int
    #: Minimum subtotal in paise for the code to be usable.
    min_subtotal: int = 0


CATALOGUE: dict[str, Promotion] = {
    "WELCOME10": Promotion(code="WELCOME10", basis_points=1000),
    "TEA5": Promotion(code="TEA5", basis_points=500),
    "BULK15": Promotion(code="BULK15", basis_points=1500, min_subtotal=100_000),
}

#: No stack of promotions may exceed 25% of the subtotal.
MAX_TOTAL_BASIS_POINTS = 2500


def lookup(code: str) -> Promotion:
    try:
        return CATALOGUE[code]
    except KeyError:
        raise InvalidPromotion(f"unknown promo code: {code}") from None


def is_applicable(promotion: Promotion, subtotal: int) -> bool:
    return subtotal >= promotion.min_subtotal


def discount_for(code: str, subtotal: int) -> int:
    """Discount in paise from a single code. Returns 0 if not applicable."""
    promotion = lookup(code)
    if not is_applicable(promotion, subtotal):
        return 0
    return subtotal * promotion.basis_points // 10_000
