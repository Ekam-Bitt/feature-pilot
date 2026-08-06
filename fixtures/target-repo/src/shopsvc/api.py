"""HTTP surface.

Thin: routes translate between JSON and the domain modules and map domain errors
onto status codes. No business logic lives here.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from shopsvc import cart as cart_module
from shopsvc import inventory
from shopsvc.errors import InsufficientStock, InvalidPromotion, UnknownSku
from shopsvc.models import Cart, Product

app = FastAPI(title="shopsvc")

CATALOGUE: dict[str, Product] = {
    "TEA-001": Product(sku="TEA-001", name="Assam tea", unit_price=25_000, category="tea"),
    "MUG-002": Product(sku="MUG-002", name="Clay mug", unit_price=20_000, category="ware"),
    "POT-003": Product(sku="POT-003", name="Teapot", unit_price=45_000, category="ware"),
    "TIN-004": Product(sku="TIN-004", name="Storage tin", unit_price=15_000, category="ware"),
    "BAG-005": Product(sku="BAG-005", name="Jute bag", unit_price=8_500, category="ware"),
}


def _product(sku: str) -> Product:
    try:
        return CATALOGUE[sku]
    except KeyError:
        raise UnknownSku(sku) from None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/products")
def list_products() -> list[dict[str, Any]]:
    return [
        {
            "sku": p.sku,
            "name": p.name,
            "unit_price": p.unit_price,
            "available": inventory.available_units(p.sku),
        }
        for p in CATALOGUE.values()
    ]


@app.post("/carts/quote")
def quote(payload: dict[str, Any]) -> dict[str, Any]:
    """Price a cart without persisting it.

    Body: {"customer_id": str, "items": [{"sku": str, "quantity": int}],
           "promo_codes": [str]}
    """
    cart = Cart(customer_id=payload.get("customer_id", "anonymous"))
    cart.promo_codes = list(payload.get("promo_codes", []))

    try:
        for line in payload.get("items", []):
            cart.add(_product(line["sku"]), int(line["quantity"]))
    except UnknownSku as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"malformed item: {exc}") from exc

    try:
        totals = cart_module.compute_totals(cart)
    except InvalidPromotion as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "subtotal": totals.subtotal,
        "quantity_discount": totals.quantity_discount,
        "promo_discount": totals.promo_discount,
        "shipping": totals.shipping,
        "total": totals.total,
    }


@app.post("/inventory/{sku}/reserve")
def reserve(sku: str, payload: dict[str, Any]) -> dict[str, Any]:
    quantity = int(payload.get("quantity", 1))
    try:
        inventory.reserve(sku, quantity)
    except InsufficientStock as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"sku": sku, "remaining": inventory.available_units(sku)}
