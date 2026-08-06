# Bulk discount doesn't apply when you buy exactly the advertised quantity

**Labels:** bug, pricing

## What happened

A customer complained that our "5 or more, save 5%" banner is lying. They put
exactly 5 boxes of Assam tea in their cart and were charged full price. When they
added a sixth box, the discount appeared — and the 6-box order came out cheaper
per unit than the 5-box one, which is what tipped them off.

Spot-checking the other tiers, the same thing happens at each threshold: the
10-unit and 20-unit tiers only kick in one unit past where the marketing says
they should.

## Steps to reproduce

1. Add 5 × `TEA-001` (₹250.00 each) to a cart.
2. Request a quote.

Subtotal is ₹1250.00 and `quantity_discount` comes back as 0.

## Expected

₹62.50 off — 5% of ₹1250.00. The tiers are documented in the README as applying
when the quantity *reaches* the threshold, so buying exactly 5 should qualify.

## Actual

No discount at all. Buying 6 gives 5%; buying 10 gives 5% instead of 10%; buying
20 gives 10% instead of 15%.

## Notes

Every tier is shifted by one, so I'd guess this is a single comparison rather than
three separate mistakes.
