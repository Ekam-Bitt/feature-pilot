"""Cart totals: discount stacking and the free-shipping threshold.

Each test asserts the narrowest field it is about. Asserting `total` everywhere
would make one defect fail tests belonging to another, which makes a failure
report useless for locating the cause.
"""

from __future__ import annotations

import pytest

from shopsvc.cart import (
    FREE_SHIPPING_THRESHOLD,
    SHIPPING_FLAT,
    compute_totals,
    promo_discount,
    shipping_for,
)
from shopsvc.models import Cart

from .conftest import BAG, MUG, TEA, TIN


def cart_of(*lines: tuple[object, int], codes: list[str] | None = None) -> Cart:
    cart = Cart(customer_id="cust-1")
    for product, quantity in lines:
        cart.add(product, quantity)  # type: ignore[arg-type]
    cart.promo_codes = list(codes or [])
    return cart


class TestSubtotal:
    def test_empty_cart(self) -> None:
        assert cart_of().subtotal == 0

    def test_sums_lines(self) -> None:
        # 2 x 25000 + 1 x 15000
        assert cart_of((TEA, 2), (TIN, 1)).subtotal == 65_000


class TestShippingBoundary:
    """`shipping_for` takes the amount the customer pays. The threshold is
    inclusive, so these pin the boundary against an off-by-one during a fix."""

    def test_just_below_threshold_pays_flat_rate(self) -> None:
        assert shipping_for(FREE_SHIPPING_THRESHOLD - 1) == SHIPPING_FLAT

    def test_exactly_at_threshold_is_free(self) -> None:
        assert shipping_for(FREE_SHIPPING_THRESHOLD) == 0

    def test_above_threshold_is_free(self) -> None:
        assert shipping_for(FREE_SHIPPING_THRESHOLD + 1) == 0


class TestPromoStacking:
    def test_single_code(self) -> None:
        assert promo_discount(cart_of((TEA, 2), codes=["WELCOME10"])) == 5_000

    def test_no_codes(self) -> None:
        assert promo_discount(cart_of((TEA, 2))) == 0

    def test_two_codes_stack_additively(self) -> None:
        """WELCOME10 (10%) + TEA5 (5%) on 50000 = 15% = 7500."""
        cart = cart_of((TEA, 2), codes=["WELCOME10", "TEA5"])
        assert promo_discount(cart) == 7_500

    def test_stack_is_capped(self) -> None:
        """10% + 5% + 15% would be 30%, capped at 25% of 100000."""
        cart = cart_of((TEA, 4), codes=["WELCOME10", "TEA5", "BULK15"])
        assert promo_discount(cart) == 25_000

    def test_inapplicable_code_contributes_nothing(self) -> None:
        # BULK15 needs a 100000 subtotal; here the subtotal is 50000.
        cart = cart_of((TEA, 2), codes=["WELCOME10", "BULK15"])
        assert promo_discount(cart) == 5_000


class TestTotalsWithoutDiscounts:
    def test_below_threshold_pays_shipping(self) -> None:
        totals = compute_totals(cart_of((TEA, 1)))
        assert totals.subtotal == 25_000
        assert totals.shipping == SHIPPING_FLAT
        assert totals.total == 30_000

    def test_exactly_at_threshold_ships_free(self) -> None:
        totals = compute_totals(cart_of((TEA, 2)))
        assert totals.subtotal == 50_000
        assert totals.shipping == 0
        assert totals.total == 50_000

    def test_well_above_threshold_ships_free(self) -> None:
        totals = compute_totals(cart_of((MUG, 1), (TIN, 1), (TEA, 2)))
        assert totals.shipping == 0


class TestQuantityDiscountInTotals:
    def test_quantity_discount_is_applied(self) -> None:
        # 6 x 25000 = 150000; 5% = 7500. Still far above the threshold.
        totals = compute_totals(cart_of((TEA, 6)))
        assert totals.quantity_discount == 7_500
        assert totals.shipping == 0
        assert totals.total == 142_500


class TestShippingUsesThePayableAmount:
    """Discounts are applied before the shipping threshold is evaluated, so an
    order can drop below the threshold and become liable for shipping."""

    def test_promo_discount_drops_order_below_threshold(self) -> None:
        # 2 x 20000 + 1 x 15000 = 55000; WELCOME10 takes 5500 -> 49500 payable.
        cart = cart_of((MUG, 2), (TIN, 1), codes=["WELCOME10"])
        totals = compute_totals(cart)
        assert totals.subtotal == 55_000
        assert totals.promo_discount == 5_500
        assert totals.shipping == SHIPPING_FLAT
        assert totals.total == 54_500

    def test_quantity_discount_drops_order_below_threshold(self) -> None:
        # 6 x 8500 = 51000; 5% = 2550 -> 48450 payable.
        totals = compute_totals(cart_of((BAG, 6)))
        assert totals.subtotal == 51_000
        assert totals.quantity_discount == 2_550
        assert totals.shipping == SHIPPING_FLAT
        assert totals.total == 53_450


class TestTotalsInvariants:
    @pytest.mark.parametrize(
        "cart",
        [
            cart_of((TEA, 1)),
            cart_of((TEA, 2)),
            cart_of((TEA, 6)),
            cart_of((BAG, 6)),
            cart_of((MUG, 2), (TIN, 1), codes=["WELCOME10"]),
        ],
    )
    def test_total_equals_its_parts(self, cart: Cart) -> None:
        """Whatever the rules decide, the breakdown must reconcile."""
        totals = compute_totals(cart)
        assert totals.total == (
            totals.subtotal - totals.quantity_discount - totals.promo_discount + totals.shipping
        )

    def test_discounts_never_exceed_the_subtotal(self) -> None:
        cart = cart_of((TEA, 4), codes=["WELCOME10", "TEA5", "BULK15"])
        totals = compute_totals(cart)
        assert totals.total_discount <= totals.subtotal
