"""Order-history queries.

Read-side only. Every query returns plain dataclasses or dicts rather than
sqlite3.Row objects, so callers don't depend on the driver.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from shopsvc.models import Order


@dataclass(frozen=True, slots=True)
class OrderSummary:
    order_id: int
    customer_id: str
    total: int
    status: str
    item_count: int


def order_history(conn: sqlite3.Connection, customer_id: str) -> list[OrderSummary]:
    """Every order belonging to `customer_id`, oldest first.

    Includes orders with no line items yet (drafts), reported with an
    `item_count` of 0 — a customer's history is not allowed to silently omit
    orders they can see in the UI.
    """
    rows = conn.execute(
        """
        SELECT o.id            AS order_id,
               o.customer_id   AS customer_id,
               o.total         AS total,
               o.status        AS status,
               COUNT(oi.id)    AS item_count
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE o.customer_id = ?
        GROUP BY o.id
        ORDER BY o.id
        """,
        (customer_id,),
    ).fetchall()
    return [
        OrderSummary(
            order_id=row["order_id"],
            customer_id=row["customer_id"],
            total=row["total"],
            status=row["status"],
            item_count=row["item_count"],
        )
        for row in rows
    ]


def lifetime_value(conn: sqlite3.Connection, customer_id: str) -> int:
    """Total paise spent on orders that were not cancelled."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(total), 0) AS value
        FROM orders
        WHERE customer_id = ? AND status != 'cancelled'
        """,
        (customer_id,),
    ).fetchone()
    return int(row["value"])


def get_order(conn: sqlite3.Connection, order_id: int) -> Order | None:
    row = conn.execute(
        "SELECT id, customer_id, total, status FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        return None
    return Order(
        order_id=row["id"],
        customer_id=row["customer_id"],
        total=row["total"],
        status=row["status"],
    )
