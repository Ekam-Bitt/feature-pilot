# Availability check crashes for a SKU that was never stocked

**Labels:** bug, inventory

## What happened

Our storefront calls the availability check before rendering the "Add to cart"
button. For a product that has been listed in the catalogue but never received a
stock shipment, the call blows up instead of reporting zero availability, and the
product page returns a 500.

This came up when merchandising pre-listed a new tin ahead of the shipment
arriving.

## Steps to reproduce

```python
from shopsvc import inventory
inventory.available_units("NOPE-999")
```

## Expected

`0`. A SKU with no ledger entry has nothing available — that's a normal state, not
an error. Downstream, `is_available("NOPE-999", 1)` should return `False`, and
attempting to reserve it should raise the usual `InsufficientStock`.

## Actual

```
TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'
```

## Notes

`stock_level()` is documented as returning `None` for an unknown SKU, so the
lookup itself looks correct — it's the caller that isn't handling it. Note that
restocking a brand-new SKU goes through the same path.
