from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from shopsvc import db, inventory
from shopsvc.models import Product


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    db.initialise(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _clean_inventory() -> Iterator[None]:
    """Stock is module-level mutable state, so every test starts from the seed
    ledger. Without this, reserve() in one test leaks into the next."""
    inventory.reset_for_tests()
    yield
    inventory.reset_for_tests()


# Prices in paise. Declared here so a test's arithmetic is readable inline
# rather than requiring a trip to the catalogue.
TEA = Product(sku="TEA-001", name="Assam tea", unit_price=25_000, category="tea")
MUG = Product(sku="MUG-002", name="Clay mug", unit_price=20_000, category="ware")
POT = Product(sku="POT-003", name="Teapot", unit_price=45_000, category="ware")
TIN = Product(sku="TIN-004", name="Storage tin", unit_price=15_000, category="ware")
BAG = Product(sku="BAG-005", name="Jute bag", unit_price=8_500, category="ware")
