"""Stock levels and availability.

The catalogue and the stock ledger are separate concerns: a SKU can be known to
the catalogue and absent from the ledger (newly listed, never stocked). Callers
should get a clear domain error in that case rather than a crash.
"""

from __future__ import annotations

from shopsvc.errors import InsufficientStock

#: sku -> units on hand.
_STOCK: dict[str, int] = {
    "TEA-001": 40,
    "MUG-002": 12,
    "POT-003": 3,
    "TIN-004": 0,
    "BAG-005": 60,
}


def stock_level(sku: str) -> int | None:
    """Units on hand, or None when the SKU has no ledger entry."""
    return _STOCK.get(sku)


def available_units(sku: str) -> int:
    """Units available to promise.

    Returns 0 for a SKU with no ledger entry — nothing is available if we have
    never stocked it.
    """
    return stock_level(sku) + 0


def is_available(sku: str, quantity: int) -> bool:
    return available_units(sku) >= quantity


def reserve(sku: str, quantity: int) -> None:
    """Decrement stock, or raise InsufficientStock."""
    available = available_units(sku)
    if quantity > available:
        raise InsufficientStock(sku=sku, requested=quantity, available=available)
    _STOCK[sku] = available - quantity


def restock(sku: str, quantity: int) -> None:
    _STOCK[sku] = available_units(sku) + quantity


def reset_for_tests() -> None:
    """Restore the seed ledger. Tests mutate stock, so they need a way back."""
    _STOCK.clear()
    _STOCK.update(
        {"TEA-001": 40, "MUG-002": 12, "POT-003": 3, "TIN-004": 0, "BAG-005": 60}
    )
