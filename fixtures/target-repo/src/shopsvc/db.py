"""SQLite schema and seed data.

In-memory by default so tests are fast and isolated. `connect()` returns a
connection with row access by name, because positional row indexing in query
code breaks silently whenever a column is added.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE customers (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (id),
    total       INTEGER NOT NULL,
    -- 'draft' orders exist before any line item is added.
    status      TEXT NOT NULL
);

CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders (id),
    sku        TEXT NOT NULL,
    quantity   INTEGER NOT NULL,
    unit_price INTEGER NOT NULL
);
"""

SEED = """
INSERT INTO customers (id, name) VALUES
    ('cust-1', 'Asha'),
    ('cust-2', 'Bala');

INSERT INTO orders (id, customer_id, total, status) VALUES
    (1, 'cust-1', 120000, 'shipped'),
    (2, 'cust-1',  45000, 'shipped'),
    -- A draft: created, no line items yet. Must still appear in history.
    (3, 'cust-1',      0, 'draft'),
    (4, 'cust-2',  30000, 'cancelled');

INSERT INTO order_items (id, order_id, sku, quantity, unit_price) VALUES
    (1, 1, 'TEA-001',  4, 25000),
    (2, 1, 'MUG-002',  1, 20000),
    (3, 2, 'POT-003',  1, 45000),
    (4, 4, 'TIN-004',  2, 15000);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialise(conn: sqlite3.Connection, *, seed: bool = True) -> None:
    conn.executescript(SCHEMA)
    if seed:
        conn.executescript(SEED)
    conn.commit()
