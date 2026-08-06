# Only the first promo code counts when a customer applies two

**Labels:** bug, promotions

## What happened

We ran a campaign where `WELCOME10` (10%) was intended to stack with the
category code `TEA5` (5%) for 15% off. Customers applied both, the UI listed both
as accepted, and the total only reflected 10%.

Nobody noticed until the campaign post-mortem, because the codes are accepted
without complaint — the discount is just short.

## Steps to reproduce

1. Cart with 2 × `TEA-001`, subtotal ₹500.00.
2. Apply `WELCOME10` and `TEA5`.

## Expected

₹75.00 off — 15% of ₹500.00. The README documents promotions as stacking
additively, subject to the 25% overall cap. So a stack of `WELCOME10` + `TEA5` +
`BULK15` on a ₹1000.00 order should be capped at ₹250.00 rather than 30%.

## Actual

₹50.00 off. Only the first code in the list is applied; the rest are silently
ignored.

## Notes

The promotions module can price one code at a time, but I don't see anything that
knows how to combine a stack or enforce the cap. Whatever computes the cart's
promo discount will need something to call.
