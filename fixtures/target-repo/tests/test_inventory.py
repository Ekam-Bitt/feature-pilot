"""Stock lookups.

The catalogue and the stock ledger are separate, so a SKU with no ledger entry is
a normal condition — not an error, and not a crash.
"""

from __future__ import annotations

import pytest

from shopsvc import inventory
from shopsvc.errors import InsufficientStock


class TestStockLevel:
    def test_known_sku_returns_units(self) -> None:
        assert inventory.stock_level("TEA-001") == 40

    def test_unknown_sku_returns_none(self) -> None:
        assert inventory.stock_level("NOPE-999") is None


class TestAvailableUnits:
    def test_known_sku(self) -> None:
        assert inventory.available_units("MUG-002") == 12

    def test_sku_stocked_at_zero(self) -> None:
        assert inventory.available_units("TIN-004") == 0

    def test_unknown_sku_is_zero_not_an_error(self) -> None:
        """Nothing is available for a SKU we have never stocked."""
        assert inventory.available_units("NOPE-999") == 0


class TestAvailability:
    def test_sufficient_stock(self) -> None:
        assert inventory.is_available("TEA-001", 10) is True

    def test_exact_stock_is_available(self) -> None:
        assert inventory.is_available("POT-003", 3) is True

    def test_insufficient_stock(self) -> None:
        assert inventory.is_available("POT-003", 4) is False

    def test_unknown_sku_is_unavailable(self) -> None:
        assert inventory.is_available("NOPE-999", 1) is False


class TestReserve:
    def test_reserve_decrements(self) -> None:
        inventory.reserve("TEA-001", 5)
        assert inventory.available_units("TEA-001") == 35

    def test_reserve_all_remaining(self) -> None:
        inventory.reserve("POT-003", 3)
        assert inventory.available_units("POT-003") == 0

    def test_over_reserve_raises(self) -> None:
        with pytest.raises(InsufficientStock) as exc:
            inventory.reserve("POT-003", 4)
        assert exc.value.available == 3
        assert exc.value.requested == 4

    def test_reserving_unknown_sku_raises(self) -> None:
        with pytest.raises(InsufficientStock):
            inventory.reserve("NOPE-999", 1)


class TestRestock:
    def test_restock_adds(self) -> None:
        inventory.restock("TIN-004", 7)
        assert inventory.available_units("TIN-004") == 7

    def test_restock_creates_a_ledger_entry(self) -> None:
        inventory.restock("NEW-777", 4)
        assert inventory.available_units("NEW-777") == 4
