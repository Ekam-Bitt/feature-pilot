"""Quantity tier behaviour.

The tiers are documented as applying *inclusively* — buying exactly the threshold
quantity earns the discount — so the boundary cases are the ones that matter.
"""

from __future__ import annotations

import pytest

from shopsvc.models import CartItem
from shopsvc.pricing import quantity_discount, tier_for, total_quantity_discount

from .conftest import BAG, TEA


class TestTierSelection:
    def test_no_tier_below_the_first_threshold(self) -> None:
        assert tier_for(4) is None

    def test_tier_applies_at_exactly_five(self) -> None:
        tier = tier_for(5)
        assert tier is not None
        assert tier.basis_points == 500

    def test_tier_applies_above_five(self) -> None:
        tier = tier_for(6)
        assert tier is not None
        assert tier.basis_points == 500

    def test_middle_tier_applies_at_exactly_ten(self) -> None:
        tier = tier_for(10)
        assert tier is not None
        assert tier.basis_points == 1000

    def test_top_tier_applies_at_exactly_twenty(self) -> None:
        tier = tier_for(20)
        assert tier is not None
        assert tier.basis_points == 1500

    def test_best_tier_wins_well_above_all_thresholds(self) -> None:
        tier = tier_for(50)
        assert tier is not None
        assert tier.basis_points == 1500


class TestDiscountAmount:
    def test_zero_below_threshold(self) -> None:
        assert quantity_discount(CartItem(product=TEA, quantity=4)) == 0

    def test_five_percent_at_the_boundary(self) -> None:
        # 5 x 25000 = 125000; 5% = 6250
        assert quantity_discount(CartItem(product=TEA, quantity=5)) == 6_250

    def test_ten_percent_at_the_boundary(self) -> None:
        # 10 x 25000 = 250000; 10% = 25000
        assert quantity_discount(CartItem(product=TEA, quantity=10)) == 25_000

    def test_fifteen_percent_at_the_boundary(self) -> None:
        # 20 x 25000 = 500000; 15% = 75000
        assert quantity_discount(CartItem(product=TEA, quantity=20)) == 75_000

    def test_rounds_down_to_whole_paise(self) -> None:
        # 6 x 8500 = 51000; 5% = 2550 exactly, so also a regression guard on
        # the integer arithmetic staying integral.
        assert quantity_discount(CartItem(product=BAG, quantity=6)) == 2_550

    @pytest.mark.parametrize("quantity", [0, 1, 2, 3, 4])
    def test_small_quantities_never_discount(self, quantity: int) -> None:
        assert quantity_discount(CartItem(product=TEA, quantity=quantity)) == 0


class TestAcrossLines:
    def test_sums_per_line_independently(self) -> None:
        items = [
            CartItem(product=TEA, quantity=5),  # 6250
            CartItem(product=BAG, quantity=1),  # 0
        ]
        assert total_quantity_discount(items) == 6_250

    def test_empty_cart_has_no_discount(self) -> None:
        assert total_quantity_discount([]) == 0
