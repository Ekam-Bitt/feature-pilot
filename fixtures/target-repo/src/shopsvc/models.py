"""Domain types.

Money is integer paise everywhere. Floats would make totals inexact and tests
non-deterministic, which is worse than the minor inconvenience of multiplying by
100 at the presentation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Product:
    sku: str
    name: str
    #: Unit price in paise.
    unit_price: int
    category: str = "general"


@dataclass(frozen=True, slots=True)
class CartItem:
    product: Product
    quantity: int

    @property
    def line_subtotal(self) -> int:
        """Price before any discount, in paise."""
        return self.product.unit_price * self.quantity


@dataclass(slots=True)
class Cart:
    customer_id: str
    items: list[CartItem] = field(default_factory=list)
    promo_codes: list[str] = field(default_factory=list)

    def add(self, product: Product, quantity: int) -> None:
        self.items.append(CartItem(product=product, quantity=quantity))

    @property
    def subtotal(self) -> int:
        return sum(item.line_subtotal for item in self.items)


@dataclass(frozen=True, slots=True)
class Totals:
    """The full breakdown of what a customer pays, all in paise."""

    subtotal: int
    quantity_discount: int
    promo_discount: int
    shipping: int
    total: int

    @property
    def total_discount(self) -> int:
        return self.quantity_discount + self.promo_discount


@dataclass(frozen=True, slots=True)
class Order:
    order_id: int
    customer_id: str
    total: int
    status: str
