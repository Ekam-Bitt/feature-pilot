"""Promo code lookup and single-code discounts.

Stacking is a cart-level concern and is covered in test_cart.py.
"""

from __future__ import annotations

import pytest

from shopsvc.errors import InvalidPromotion
from shopsvc.promotions import CATALOGUE, discount_for, is_applicable, lookup


class TestLookup:
    def test_known_code(self) -> None:
        assert lookup("WELCOME10").basis_points == 1000

    def test_unknown_code_raises(self) -> None:
        with pytest.raises(InvalidPromotion, match="unknown promo code"):
            lookup("NOPE")


class TestApplicability:
    def test_code_without_a_minimum_always_applies(self) -> None:
        assert is_applicable(CATALOGUE["WELCOME10"], 1) is True

    def test_code_with_a_minimum_below_it(self) -> None:
        assert is_applicable(CATALOGUE["BULK15"], 99_999) is False

    def test_minimum_is_inclusive(self) -> None:
        assert is_applicable(CATALOGUE["BULK15"], 100_000) is True


class TestSingleCodeDiscount:
    def test_ten_percent(self) -> None:
        assert discount_for("WELCOME10", 50_000) == 5_000

    def test_five_percent(self) -> None:
        assert discount_for("TEA5", 50_000) == 2_500

    def test_inapplicable_code_discounts_nothing(self) -> None:
        assert discount_for("BULK15", 50_000) == 0

    def test_rounds_down(self) -> None:
        # 5% of 1999 is 99.95 paise; the customer keeps the fraction.
        assert discount_for("TEA5", 1_999) == 99
