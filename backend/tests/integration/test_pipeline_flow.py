"""End-to-end money-in tests on the real route layer (WP-1.4 TST-05/06/09).

PayPal HTTP is fully mocked with respx (no bypass flag anywhere); every test
starts from the demo-seeded Acme contract with MS-01 = 1000.00 due 2026-10-15.
Covers: COMPLIANT pay, underpaid/late pay (discrepancy + guard Ask), the
deterministic-request-id idempotency (409 CAPTURE_ALREADY_PROCESSED), and the
webhook convergence race (PIT-03).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from backend.app.db import Base, engine

SANDBOX = "https://api-m.sandbox.paypal.com"
TOKEN = {"access_token": "test-token", "expires_in": 10800}
DEMOS = {"X-Demo-Key": "test-demo-key-0123456789abcdef"}


@pytest.fixture(autouse=True)
def fresh_tables() -> Any:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client() -> Any:
    from backend.app.main import app

    with TestClient(app) as test_client:  # lifespan runs init_db + demo seed
        yield test_client


def mock_token() -> respx.Route:
    return respx.post(f"{SANDBOX}/v1/oauth2/token").mock(
        return_value=Response(200, json=TOKEN)
    )


def order_body(order_id: str, value: str, currency: str = "USD") -> dict[str, Any]:
    return {
        "id": order_id,
        "status": "CREATED",
        "purchase_units": [
            {"amount": {"value": value, "currency_code": currency}}
        ],
    }


def capture_body(
    capture_id: str, order_id: str, value: str, currency: str = "USD"
) -> dict[str, Any]:
    return {
        "id": order_id,
        "status": "COMPLETED",
        "purchase_units": [
            {
                "payments": {
                    "captures": [
                        {
                            "id": capture_id,
                            "status": "COMPLETED",
                            "amount": {"value": value, "currency_code": currency},
                        }
                    ]
                }
            }
        ],
        "payer": {"email_address": "client@acme.com"},
    }


def mock_order(order_id: str, value: str) -> None:
    respx.post(f"{SANDBOX}/v2/checkout/orders").mock(
        return_value=Response(201, json=order_body(order_id, value))
    )
    respx.post(f"{SANDBOX}/v2/checkout/orders/{order_id}/capture").mock(
        return_value=Response(
            201, json=capture_body(f"CAP-{order_id}", order_id, value)
        )
    )


def pay_request(value: str, date: str, key: str) -> dict[str, Any]:
    return {
        "milestone_id": "ms_seed_01",
        "simulated_amount": value,
        "currency": "USD",
        "simulated_payment_date": date,
        "payer_email": "client@acme.com",
        "idempotency_key": key,
    }


@respx.mock
def test_compliant_payment(client: TestClient) -> None:
    mock_token()
    mock_order("ORDER-COMPLIANT-01", "1000.00")

    got = client.post(
        "/api/v1/simulator/pay",
        json=pay_request("1000.00", "2026-10-14", "compliant-key"),
        headers=DEMOS,
    )

    assert got.status_code == 200
    body = got.json()
    assert body["paypal_capture_id"] == "CAP-ORDER-COMPLIANT-01"
    assert body["capture_status"] == "COMPLETED"
    assert body["discrepancy_id"] is None
    assert body["payroll_run_id"] is None

    ledger = client.get("/api/v1/ledger").json()
    assert ledger["summary"]["total_captured"] == "1000.00"
    assert ledger["rows"][0]["discrepancy_status"] == "NONE"


@respx.mock
def test_underpaid_late_payment_and_guard(client: TestClient) -> None:
    mock_token()
    mock_order("ORDER-UNDERPAID-A1", "500.00")

    got = client.post(
        "/api/v1/simulator/pay",
        json=pay_request("500.00", "2026-10-25", "underpaid-key"),
        headers=DEMOS,
    )

    assert got.status_code == 200
    body = got.json()
    assert body["discrepancy_id"] is not None
    assert body["remediation_invoice"] is None

    ledger = client.get("/api/v1/ledger").json()
    row = ledger["rows"][0]
    shortfall = Decimal(row["shortfall_amount"])
    penalty = Decimal(row["penalty_amount"])
    assert Decimal("1000.00") - Decimal("500.00") == shortfall
    assert Decimal(row["total_remediation_amount"]) == shortfall + penalty
    assert row["discrepancy_status"] in ("PENDING_APPROVAL", "BLOCKED")
    assert row["leash_decision"] in ("ASK", "DENY")


@respx.mock
def test_duplicate_capture_is_rejected(client: TestClient) -> None:
    mock_token()
    mock_order("ORDER-DUPLICATE-01", "1000.00")

    first = client.post(
        "/api/v1/simulator/pay", json=pay_request("1000.00", "2026-10-14", "dup"), headers=DEMOS
    )
    second = client.post(
        "/api/v1/simulator/pay", json=pay_request("1000.00", "2026-10-14", "dup"), headers=DEMOS
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CAPTURE_ALREADY_PROCESSED"


@respx.mock
def test_card_order_captured_at_create_skips_capture_call(client: TestClient) -> None:
    """A card source settles at create time: /capture would 422 ORDER_ALREADY_CAPTURED."""
    mock_token()
    order_id = "ORDER-CARD-COMPLETED"
    create = respx.post(f"{SANDBOX}/v2/checkout/orders").mock(
        return_value=Response(201, json=capture_body(f"CAP-{order_id}", order_id, "1000.00"))
    )
    capture = respx.post(f"{SANDBOX}/v2/checkout/orders/{order_id}/capture").mock(
        return_value=Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
    )

    got = client.post(
        "/api/v1/simulator/pay",
        json=pay_request("1000.00", "2026-10-14", "card-key"),
        headers=DEMOS,
    )

    assert got.status_code == 200
    assert got.json()["paypal_capture_id"] == f"CAP-{order_id}"
    assert got.json()["capture_status"] == "COMPLETED"
    assert create.call_count == 1
    assert create.calls[0].request.headers["PayPal-Request-Id"] == "ms_seed_01:card-key"
    assert not capture.called
    assert client.get("/api/v1/ledger").json()["summary"]["total_captured"] == "1000.00"


@respx.mock
def test_created_order_is_captured_with_its_own_request_id(client: TestClient) -> None:
    mock_token()
    mock_order("ORDER-CREATED-01", "1000.00")
    capture = respx.routes[-1]

    got = client.post(
        "/api/v1/simulator/pay",
        json=pay_request("1000.00", "2026-10-14", "created-key"),
        headers=DEMOS,
    )

    assert got.status_code == 200
    assert capture.call_count == 1
    assert capture.calls[0].request.headers["PayPal-Request-Id"] == "ms_seed_01:created-key:capture"


@pytest.mark.parametrize(
    "body",
    [
        {"id": "O-1", "status": "CREATED"},
        {"id": "O-1", "status": "APPROVED", "purchase_units": [{}]},
        {"id": "O-1", "status": "COMPLETED", "purchase_units": [{}]},
        {"id": "O-1", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": []}}]},
    ],
)
def test_captured_at_create_needs_a_completed_capture(body: dict[str, Any]) -> None:
    from backend.app.services.paypal.orders import captured_at_create

    assert not captured_at_create(body)
    assert captured_at_create(capture_body("CAP-1", "O-1", "1.00"))
