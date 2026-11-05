"""Webhook flow tests: signature gate, dedupe, capture convergence (PIT-03, R2).

Every delivery here is signed by a mocked ``verify-webhook-signature``
responding SUCCESS; missing headers answer 400. The capture-first rule holds:
the webhook may own the claim (source WEBHOOK) or converge only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from backend.app.db import Base, engine

SANDBOX = "https://api-m.sandbox.paypal.com"
TOKEN = {"access_token": "test-token", "expires_in": 10800}

TRANSMISSION_HEADERS = {
    "PayPal-Auth-Algo": "SHA256withRSA",
    "PayPal-Cert-Url": "https://api.paypal.com/v1/notifications/certs/CERT-36064242AAA",
    "PayPal-Transmission-Id": "TR-01",
    "PayPal-Transmission-Sig": "sig-01",
    "PayPal-Transmission-Time": "2026-11-03T12:00:00Z",
}


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


def verify_mock(status: str = "SUCCESS") -> None:
    respx.post(f"{SANDBOX}/v1/notifications/verify-webhook-signature").mock(
        return_value=Response(200, json={"verification_status": status})
    )
    respx.post(f"{SANDBOX}/v1/oauth2/token").mock(
        return_value=Response(200, json=TOKEN)
    )


def capture_event(capture_id: str, value: str, custom: str = "ms_seed_01") -> dict[str, Any]:
    return {
        "id": f"WH-{capture_id}",
        "version": "1.0",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": {"value": value, "currency_code": "USD"},
            "custom_id": custom,
            "update_time": "2026-10-25T10:00:00Z",
            "payer": {"email_address": "client@acme.com"},
            "supplementary_data": {
                "related_ids": {"order_id": f"ORDER-{capture_id}"}
            },
        },
    }


@respx.mock
def test_webhook_signature_then_capture_audits(client: TestClient) -> None:
    verify_mock()
    event = capture_event("CAP-WEBHOOK-01", "500.00")
    headers = {"Content-Type": "application/json", **TRANSMISSION_HEADERS}

    first = client.post("/webhooks/paypal", content=json.dumps(event), headers=headers)
    assert first.status_code == 200
    assert first.json() == {"received": True}

    ledger = client.get("/ledger").json()
    row = ledger["rows"][0]
    assert row["paypal_capture_id"] == "CAP-WEBHOOK-01"
    assert row["webhook_verified"] is True
    assert row["discrepancy_status"] in ("PENDING_APPROVAL", "BLOCKED")

    repeat = client.post("/webhooks/paypal", content=json.dumps(event), headers=headers)
    assert repeat.status_code == 200

    missing = client.post(
        "/webhooks/paypal", content=json.dumps(event), headers={"Content-Type": "application/json"}
    )
    assert missing.status_code == 400


@respx.mock
def test_webhook_bad_signature_is_refused(client: TestClient) -> None:
    verify_mock("FAILURE")
    got = client.post(
        "/webhooks/paypal",
        content=json.dumps(capture_event("CAP-WEBHOOK-02", "500.00")),
        headers={"Content-Type": "application/json", **TRANSMISSION_HEADERS},
    )
    assert got.status_code == 400
    assert got.json()["error"]["code"] == "WEBHOOK_SIGNATURE_INVALID"
