# Free shipping is granted on orders that fall below the threshold after discounts

**Labels:** bug, shipping, revenue

## What happened

Finance flagged that we're absorbing shipping on orders we shouldn't be. Orders
that clear the ₹500.00 free-shipping threshold *before* discounts are getting
free shipping even when the customer ends up paying less than ₹500.00.

We're eating ₹50.00 per affected order, and it's a meaningful number over a
promotional weekend.

## Steps to reproduce

Case A — promo discount:

1. Cart with 2 × `MUG-002` and 1 × `TIN-004`. Subtotal ₹550.00.
2. Apply `WELCOME10` (10% = ₹55.00). Customer pays ₹495.00.

Case B — quantity discount:

1. Cart with 6 × `BAG-005`. Subtotal ₹510.00.
2. The 5% bulk tier takes ₹25.50. Customer pays ₹484.50.

## Expected

Both orders are charged the ₹50.00 flat rate. The README states that discounts
apply before the shipping threshold is evaluated — eligibility follows what the
customer actually pays.

An order that lands on exactly ₹500.00 after discounts still ships free; the
threshold is inclusive and shouldn't move.

## Actual

Both ship free. The threshold is being evaluated against the pre-discount
subtotal.

## Notes

Worth checking both discount kinds — they're computed in different places and I'm
not confident a fix for one covers the other.
