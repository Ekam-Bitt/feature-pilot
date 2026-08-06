"""Domain exceptions.

Callers distinguish "you asked for something that doesn't exist" from "you asked
for something impossible", so these are separate types rather than one
ValueError.
"""


class ShopError(Exception):
    """Base class for every domain error."""


class UnknownSku(ShopError):
    """No product exists with the requested SKU."""

    def __init__(self, sku: str) -> None:
        super().__init__(f"unknown sku: {sku}")
        self.sku = sku


class InsufficientStock(ShopError):
    """The requested quantity exceeds what is on hand."""

    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(
            f"insufficient stock for {sku}: requested {requested}, available {available}"
        )
        self.sku = sku
        self.requested = requested
        self.available = available


class InvalidPromotion(ShopError):
    """The promo code does not exist or is not applicable."""
