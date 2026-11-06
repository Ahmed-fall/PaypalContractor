"""Owner remediation flow (WP-1.4 + R3): confirm_send, PIT-11 draft, 409s.

The discrepancy comes from a 500.00 simulator pay (guard Ask →
PENDING_APPROVAL). The owner route then runs P2's engine: draft create →
persist (PIT-11) → send → QR → REMEDIATED. A repeat call gets 409
ALREADY_REMEDIATED; confirm_send false is refused before any PayPal call.

Note: the set-up flow runs inside the ``@respx.mock`` test body (fixtures run
before the respx context, so any PayPal call there would hit the real sandbox).
"""

from __future__ import annotations

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

    with TestClient(app) as test_client:
        yield test_client


def mock_everything() -> None:
    """Token + simulator capture + the entire invoice run (superset of steps)."""
    respx.post(f"{SANDBOX}/v1/oauth2/token").mock(
        return_value=Response(200, json=TOKEN)
    )
    respx.post(f"{SANDBOX}/v2/checkout/orders").mock(
        return_value=Response(
            201,
            json={
                "id": "ORDER-REMEDIATE-A1",
                "status": "CREATED",
                "purchase_units": [
                    {"amount": {"value": "500.00", "currency_code": "USD"}}
                ],
            },
        )
    )
    respx.post(f"{SANDBOX}/v2/checkout/orders/ORDER-REMEDIATE-A1/capture").mock(
        return_value=Response(
            201,
            json={
                "id": "CAP-REMEDIATE-A1",
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "payments": {
                            "captures": [
                                {
                                    "id": "CAP-REMEDIATE-A1",
                                    "status": "COMPLETED",
                                    "amount": {"value": "500.00", "currency_code": "USD"},
                                }
                            ]
                        }
                    }
                ],
                "payer": {"email_address": "client@acme.com"},
            },
        )
    )
    respx.post(f"{SANDBOX}/v2/invoicing/invoices").mock(
        # PayPal's default create response is a link, not the invoice body.
        return_value=Response(
            201,
            json={
                "rel": "self",
                "href": f"{SANDBOX}/v2/invoicing/invoices/INV-TEST-REMEDIATION-01",
                "method": "GET",
            },
        )
    )
    respx.post(f"{SANDBOX}/v2/invoicing/invoices/INV-TEST-REMEDIATION-01/send").mock(
        return_value=Response(200, json={})
    )
    respx.post(
        f"{SANDBOX}/v2/invoicing/invoices/INV-TEST-REMEDIATION-01/generate-qr-code"
    ).mock(return_value=Response(200, json={"image_as_base64": "Q0k="}))
    respx.get(f"{SANDBOX}/v2/invoicing/invoices/INV-TEST-REMEDIATION-01").mock(
        return_value=Response(
            200,
            json={
                "id": "INV-TEST-REMEDIATION-01",
                "links": [
                    {
                        "rel": "payer_view",
                        "href": "https://demo.example.com/payer-view/INV-TEST-REMEDIATION-01",
                    }
                ],
            },
        )
    )


def pay_underpaid(client: TestClient) -> str:
    got = client.post(
        "/simulator/pay",
        json={
            "milestone_id": "ms_seed_01",
            "simulated_amount": "500.00",
            "currency": "USD",
            "simulated_payment_date": "2026-10-25",
            "payer_email": "client@acme.com",
            "idempotency_key": "remediate-key",
        },
        headers=DEMOS,
    )
    assert got.status_code == 200
    discrepancy_id = got.json()["discrepancy_id"]
    assert discrepancy_id is not None
    return discrepancy_id


@respx.mock
def test_owner_remediation_send(client: TestClient) -> None:
    mock_everything()
    dsc = pay_underpaid(client)

    refused = client.post(
        f"/discrepancies/{dsc}/remediate", json={"confirm_send": False}, headers=DEMOS
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "REMEDIATION_NOT_CONFIRMED"

    accepted = client.post(
        f"/discrepancies/{dsc}/remediate", json={"confirm_send": True}, headers=DEMOS
    )
    assert accepted.status_code == 200, accepted.json()
    payload = accepted.json()
    assert payload["discrepancy_id"] == dsc
    assert payload["discrepancy_status"] == "REMEDIATED"
    invoice = payload["remediation_invoice"]
    assert invoice["paypal_invoice_id"] == "INV-TEST-REMEDIATION-01"
    assert invoice["status"] == "SENT"
    assert invoice["qr_code_base64"] == "Q0k="

    ledger = client.get("/ledger").json()
    row = ledger["rows"][0]
    assert row["discrepancy_status"] == "REMEDIATED"
    assert row["remediation_sent_by"] == "owner"
    assert ledger["summary"]["total_remediation_invoiced"] == str(row["total_remediation_amount"])


@respx.mock
def test_repeated_remediation_is_refused(client: TestClient) -> None:
    mock_everything()
    dsc = pay_underpaid(client)
    first = client.post(
        f"/discrepancies/{dsc}/remediate", json={"confirm_send": True}, headers=DEMOS
    )
    assert first.status_code == 200

    again = client.post(
        f"/discrepancies/{dsc}/remediate", json={"confirm_send": True}, headers=DEMOS
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ALREADY_REMEDIATED"
