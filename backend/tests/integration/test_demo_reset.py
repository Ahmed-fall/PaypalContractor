"""Demo seed + reset cover Leash: a pay right after boot gets an H2 run, and
``/api/v1/demo/reset`` clears it (FK-safe) while keeping the decision log."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select

from backend.app.config import get_settings
from backend.app.db import Base, engine
from backend.app.db.database import SessionLocal
from backend.app.db.models import Discrepancy, PaypalCapture
from backend.app.leash.models import (
    LeashDecisionRow,
    LeashPolicyRow,
    PayrollLineRow,
    PayrollRunRow,
    TeamMemberRow,
    TimesheetEntryRow,
)
from backend.app.leash.payroll import runs

SANDBOX = "https://api-m.sandbox.paypal.com"
DEMOS = {"X-Demo-Key": "test-demo-key-0123456789abcdef"}


@pytest.fixture(autouse=True)
def fresh_tables(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(get_settings(), "seed_demo_data", True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client() -> Any:
    from backend.app.main import app

    with TestClient(app) as test_client:  # lifespan seeds Detective + Leash
        yield test_client


def count(model: type[Any]) -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def mock_paypal(order_id: str) -> None:
    respx.post(f"{SANDBOX}/v1/oauth2/token").mock(
        return_value=Response(200, json={"access_token": "test-token", "expires_in": 10800})
    )
    respx.post(f"{SANDBOX}/v2/checkout/orders").mock(
        return_value=Response(
            201,
            json={
                "id": order_id,
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "payments": {
                            "captures": [
                                {
                                    "id": f"CAP-{order_id}",
                                    "status": "COMPLETED",
                                    "amount": {"value": "1000.00", "currency_code": "USD"},
                                }
                            ]
                        }
                    }
                ],
                "payer": {"email_address": "client@acme.com"},
            },
        )
    )


def pay(client: TestClient, key: str) -> str:
    got = client.post(
        "/api/v1/simulator/pay",
        json={
            "milestone_id": "ms_seed_01",
            "simulated_amount": "1000.00",
            "currency": "USD",
            "simulated_payment_date": "2026-10-14",
            "payer_email": "client@acme.com",
            "idempotency_key": key,
        },
        headers=DEMOS,
    )
    assert got.status_code == 200, got.text
    client.portal.call(runs.wait_for_background)
    run_id = got.json()["payroll_run_id"]
    assert isinstance(run_id, str)
    return run_id


@respx.mock
def test_boot_seed_lets_the_first_pay_create_a_run(client: TestClient) -> None:
    assert count(TeamMemberRow) == 3
    assert count(TimesheetEntryRow) == 9
    mock_paypal("ORDER-BOOT-01")
    run_id = pay(client, "boot")
    with SessionLocal() as db:
        run = db.get(PayrollRunRow, run_id)
        assert run is not None and run.milestone_id == "ms_seed_01"


@respx.mock
def test_reset_clears_leash_state_and_keeps_decisions(client: TestClient) -> None:
    mock_paypal("ORDER-RESET-01")
    first = pay(client, "take-1")
    assert count(PayrollLineRow) == 3
    decisions = count(LeashDecisionRow)
    assert decisions > 0

    reset = client.post("/api/v1/demo/reset", headers=DEMOS)

    assert reset.status_code == 200
    wiped = reset.json()["wiped"]
    assert wiped["payroll_runs"] == 1 and wiped["payroll_lines"] == 3
    assert wiped["team_members"] == 3 and wiped["milestones"] == 1
    assert count(PayrollRunRow) == 0
    assert count(TeamMemberRow) == 3  # re-seeded
    assert count(TimesheetEntryRow) == 9
    assert count(LeashDecisionRow) == decisions  # append-only, kept

    respx.reset()
    mock_paypal("ORDER-RESET-02")
    second = pay(client, "take-2")
    assert second != first
    assert count(PayrollRunRow) == 1


@respx.mock
def test_reset_after_a_discrepancy_wipes_in_fk_order(client: TestClient) -> None:
    """A short or late pay leaves a discrepancy pointing at its capture: the reset
    must delete discrepancies before captures (live Nov 7: FK failure, 500)."""
    mock_paypal("ORDER-RESET-FK")
    pay(client, "take-fk")
    with SessionLocal() as db, db.begin():
        capture = db.scalars(select(PaypalCapture)).one()
        db.add(
            Discrepancy(
                id="dsc_TEST_FK",
                milestone_id=capture.milestone_id,
                capture_id=capture.id,
                discrepancy_type="UNDERPAID_AND_LATE",
                shortfall_amount=Decimal("150.00"),
                penalty_amount=Decimal("50.00"),
                total_remediation_amount=Decimal("200.00"),
                ai_audit_summary="test",
            )
        )

    reset = client.post("/api/v1/demo/reset", headers=DEMOS)

    assert reset.status_code == 200, reset.text
    assert count(Discrepancy) == 0 and count(PaypalCapture) == 0
    assert count(TeamMemberRow) == 3  # Leash re-seeded after the full wipe


@respx.mock
def test_reset_restores_the_seeded_policy(client: TestClient) -> None:
    with SessionLocal() as db, db.begin():
        db.execute(LeashPolicyRow.__table__.update().values(active=False))
        db.add(
            LeashPolicyRow(
                id="lpol_TEST_V2",
                version=2,
                source_text="stricter",
                compiled_json={},
                active=True,
            )
        )

    assert client.post("/api/v1/demo/reset", headers=DEMOS).status_code == 200

    with SessionLocal() as db:
        active = db.scalars(select(LeashPolicyRow.version).where(LeashPolicyRow.active)).all()
    assert list(active) == [1]
    assert count(LeashPolicyRow) == 2
