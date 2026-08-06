# shopsvc

A small order service: carts, quantity pricing, promotions, inventory, and order
history. Money is handled in integer **paise** end to end — never floats — so
totals are exact and tests are deterministic.

## Layout

| Module | Responsibility |
|---|---|
| `models.py` | Domain types: `Product`, `CartItem`, `Cart`, `Order` |
| `pricing.py` | Quantity discount tiers |
| `promotions.py` | Promo codes and how they stack |
| `cart.py` | Cart totals: subtotal, discounts, shipping, grand total |
| `inventory.py` | Stock levels and availability checks |
| `db.py` | SQLite schema and seed data |
| `repository.py` | Order-history queries |
| `api.py` | FastAPI routes |
| `errors.py` | Domain exceptions |

## Rules that matter

- **Quantity tiers** apply at a threshold *inclusively*: buying exactly the
  threshold quantity earns the discount.
- **Discounts apply before the shipping threshold is evaluated.** A cart is
  eligible for free shipping based on what the customer actually pays.
- **Promotions stack additively**, capped at a maximum total discount.

## Running

```bash
pip install -e '.[dev]'
pytest
```
