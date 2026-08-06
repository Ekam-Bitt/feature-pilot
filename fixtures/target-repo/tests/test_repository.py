"""Order-history queries.

The seed data deliberately includes a draft order with no line items, because
that is the case a naive join drops.
"""

from __future__ import annotations

import sqlite3

from shopsvc.repository import get_order, lifetime_value, order_history


class TestOrderHistory:
    def test_returns_every_order_for_the_customer(self, conn: sqlite3.Connection) -> None:
        """cust-1 has three orders: two shipped and one draft."""
        history = order_history(conn, "cust-1")
        assert [o.order_id for o in history] == [1, 2, 3]

    def test_includes_a_draft_with_no_items(self, conn: sqlite3.Connection) -> None:
        """A customer's history may not silently omit an order they can see."""
        history = order_history(conn, "cust-1")
        draft = next(o for o in history if o.order_id == 3)
        assert draft.status == "draft"
        assert draft.item_count == 0

    def test_counts_line_items(self, conn: sqlite3.Connection) -> None:
        history = order_history(conn, "cust-1")
        by_id = {o.order_id: o for o in history}
        assert by_id[1].item_count == 2
        assert by_id[2].item_count == 1

    def test_does_not_return_one_row_per_item(self, conn: sqlite3.Connection) -> None:
        """Order 1 has two items and must still appear exactly once."""
        history = order_history(conn, "cust-1")
        assert [o.order_id for o in history].count(1) == 1

    def test_scoped_to_the_requested_customer(self, conn: sqlite3.Connection) -> None:
        assert [o.order_id for o in order_history(conn, "cust-2")] == [4]

    def test_unknown_customer_has_empty_history(self, conn: sqlite3.Connection) -> None:
        assert order_history(conn, "cust-nope") == []

    def test_ordered_oldest_first(self, conn: sqlite3.Connection) -> None:
        history = order_history(conn, "cust-1")
        assert [o.order_id for o in history] == sorted(o.order_id for o in history)


class TestLifetimeValue:
    def test_sums_non_cancelled_orders(self, conn: sqlite3.Connection) -> None:
        # 120000 shipped + 45000 shipped + 0 draft
        assert lifetime_value(conn, "cust-1") == 165_000

    def test_excludes_cancelled_orders(self, conn: sqlite3.Connection) -> None:
        assert lifetime_value(conn, "cust-2") == 0

    def test_unknown_customer_is_zero(self, conn: sqlite3.Connection) -> None:
        assert lifetime_value(conn, "cust-nope") == 0


class TestGetOrder:
    def test_returns_the_order(self, conn: sqlite3.Connection) -> None:
        order = get_order(conn, 1)
        assert order is not None
        assert order.customer_id == "cust-1"
        assert order.total == 120_000

    def test_missing_order_is_none(self, conn: sqlite3.Connection) -> None:
        assert get_order(conn, 999) is None
