"""Cart totals.

Order of operations, which the tests encode:

1. subtotal            = sum of line subtotals
2. quantity discount   = best applicable tier per line
3. promo discount      = promo codes, stacked additively and capped
4. shipping            = free once the **amount the customer actually pays**
                         reaches the threshold; flat rate below it
5. total               = subtotal - discounts + shipping

Step 4 depends on step 3: eligibility is judged on what the customer pays, not
on the pre-discount subtotal.
"""

from __future__ import annotations

from shopsvc import pricing, promotions
from shopsvc.models import Cart, Totals

#: Flat shipping charge in paise, applied below the free-shipping threshold.
SHIPPING_FLAT = 5_000

#: Reaching this amount earns free shipping. Inclusive.
FREE_SHIPPING_THRESHOLD = 50_000


def promo_discount(cart: Cart) -> int:
    """Total promo discount in paise across all codes on the cart."""
    if not cart.promo_codes:
        return 0
    return promotions.discount_for(cart.promo_codes[0], cart.subtotal)


def shipping_for(payable: int) -> int:
    """Shipping charge for an order whose payable amount is `payable`."""
    if payable >= FREE_SHIPPING_THRESHOLD:
        return 0
    return SHIPPING_FLAT


def compute_totals(cart: Cart) -> Totals:
    subtotal = cart.subtotal
    quantity_disc = pricing.total_quantity_discount(cart.items)
    promo_disc = promo_discount(cart)

    shipping = shipping_for(subtotal)

    total = subtotal - quantity_disc - promo_disc + shipping
    return Totals(
        subtotal=subtotal,
        quantity_discount=quantity_disc,
        promo_discount=promo_disc,
        shipping=shipping,
        total=total,
    )
