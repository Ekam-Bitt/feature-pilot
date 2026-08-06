# Draft orders are missing from order history

**Labels:** bug, orders

## What happened

Support escalated this: a customer started an order, saw it in their account, then
came back later and it had vanished from their history. The order still exists —
you can pull it up directly by ID — it just doesn't appear in the list.

Every affected order turned out to be one that had no line items yet.

## Steps to reproduce

Using the seed data:

1. `cust-1` has three orders: two shipped (1 and 2) and one draft (3, no items).
2. Call `order_history(conn, "cust-1")`.

## Expected

Three summaries, oldest first, with the draft reported as `item_count=0`. A
customer's history shouldn't silently omit an order they can see elsewhere in the
UI.

## Actual

Two summaries. Order 3 is absent entirely.

## Notes

Lifetime value looks right, so this seems specific to the history query rather
than the underlying data.
