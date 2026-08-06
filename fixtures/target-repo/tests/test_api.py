"""HTTP surface.

Smoke coverage only: the routes are thin, and the pricing rules are asserted
directly in the unit tests where a failure points at the rule rather than at the
transport.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shopsvc.api import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


class TestHealth:
    def test_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestProducts:
    def test_lists_catalogue_with_availability(self, client: TestClient) -> None:
        response = client.get("/products")
        assert response.status_code == 200
        body = response.json()
        skus = {item["sku"] for item in body}
        assert {"TEA-001", "MUG-002", "POT-003", "TIN-004"} <= skus
        assert all("available" in item for item in body)


class TestQuote:
    def test_prices_a_simple_cart(self, client: TestClient) -> None:
        response = client.post(
            "/carts/quote",
            json={"customer_id": "cust-1", "items": [{"sku": "TEA-001", "quantity": 1}]},
        )
        assert response.status_code == 200
        assert response.json()["subtotal"] == 25_000

    def test_unknown_sku_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/carts/quote",
            json={"items": [{"sku": "NOPE-999", "quantity": 1}]},
        )
        assert response.status_code == 404

    def test_malformed_item_is_422(self, client: TestClient) -> None:
        response = client.post("/carts/quote", json={"items": [{"sku": "TEA-001"}]})
        assert response.status_code == 422

    def test_unknown_promo_code_is_400(self, client: TestClient) -> None:
        response = client.post(
            "/carts/quote",
            json={
                "items": [{"sku": "TEA-001", "quantity": 1}],
                "promo_codes": ["NOT-A-CODE"],
            },
        )
        assert response.status_code == 400

    def test_empty_cart_quotes_zero_subtotal(self, client: TestClient) -> None:
        response = client.post("/carts/quote", json={"items": []})
        assert response.status_code == 200
        assert response.json()["subtotal"] == 0


class TestReserve:
    def test_reserves_stock(self, client: TestClient) -> None:
        response = client.post("/inventory/TEA-001/reserve", json={"quantity": 2})
        assert response.status_code == 200
        assert response.json()["remaining"] == 38

    def test_over_reserve_is_409(self, client: TestClient) -> None:
        response = client.post("/inventory/POT-003/reserve", json={"quantity": 99})
        assert response.status_code == 409
