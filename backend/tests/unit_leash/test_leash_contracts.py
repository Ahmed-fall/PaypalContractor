import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.schemas import contracts as c

G01_WHEN = {"amount_lte": "250.00", "confidence_gte": 0.9, "clause_quote_present": True}


def test_policy_defaults() -> None:
    p = c.LeashPolicy(version=1, rules=[])
    assert p.payroll_margin_limit_pct == Decimal(80) and p.email_change_window_days == 7


def test_rule_when_preserves_value_types() -> None:
    r = c.LeashRule(
        id="G-01",
        applies_to="SEND_REMEDIATION_INVOICE",
        when={**G01_WHEN, "invoiced_same_client_within_days": 7},
        outcome="ALLOW",
        source_text="x",
    )
    assert r.when["amount_lte"] == "250.00" and isinstance(r.when["amount_lte"], str)
    assert r.when["clause_quote_present"] is True
    assert r.when["invoiced_same_client_within_days"] == 7
    assert type(r.when["invoiced_same_client_within_days"]) is int
    assert type(r.when["confidence_gte"]) is float


@pytest.mark.parametrize(
    "bad",
    [
        250.0,
        250,
        "abc",
        "250.001",
        True,
        "NaN",
        "Infinity",
        "-1.00",
        " 250.00 ",
        "1_000.00",
        "-0.00",
        "1E+2",
        "250",
        "250.0",
    ],
)
def test_rule_amount_threshold_must_be_decimal_string(bad: object) -> None:
    with pytest.raises(ValidationError):
        c.LeashRule(
            id="G-02",
            applies_to="SEND_REMEDIATION_INVOICE",
            when={"amount_gt": bad},  # type: ignore[dict-item]
            outcome="ASK",
            source_text="x",
        )


@pytest.mark.parametrize("key", ["amount", "max_amount_usd"])
def test_any_amount_key_is_checked(key: str) -> None:
    with pytest.raises(ValidationError):
        c.LeashRule(
            id="G-02",
            applies_to="SEND_REMEDIATION_INVOICE",
            when={key: 250.0},
            outcome="ASK",
            source_text="x",
        )


def test_allow_rule_requires_a_condition() -> None:
    with pytest.raises(ValidationError):
        c.LeashRule(
            id="G-09",
            applies_to="SEND_REMEDIATION_INVOICE",
            when={},
            outcome="ALLOW",
            source_text="x",
        )
    ok = c.LeashRule(
        id="G-09",
        applies_to="SEND_REMEDIATION_INVOICE",
        when={},
        outcome="ASK",
        source_text="x",
    )
    assert ok.when == {}


def test_guard_result_reason_capped_at_200() -> None:
    with pytest.raises(ValidationError):
        c.GuardResult(
            decision="ALLOW", rule_ids=["G-01"], reason="x" * 201, decision_id="lsd_1"
        )


def test_run_out_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        c.PayrollRunOut(
            run_id="prn_1",
            mode="MILESTONE",
            milestone_id="ms_01",
            status="DONE",
            total="0.00",
            cash_in="0.00",
            cash_used_pct="0.00",
            run_decision="ALLOW",
            run_flags=[],
            lines=[],
        )


def test_payroll_line_money_serializes_as_string() -> None:
    line = c.PayrollLineOut(
        line_id="prl_1",
        member_name="Sara",
        payout_email="sb-sara@personal.example.com",
        hours="10.00",
        rate="22.00",
        gross="220.00",
        decision="ALLOW",
        flags=[],
    )
    out = json.loads(line.model_dump_json())
    assert (
        out["gross"] == "220.00"
        and out["hours"] == "10.00"
        and out["owner_action"] is None
    )


def test_mapping_proposal_rejects_unknown_target() -> None:
    with pytest.raises(ValidationError):
        c.ColumnMappingProposal(mapping={"Pay": "salary"}, confidence={"Pay": 0.5})  # type: ignore[dict-item]


def test_team_member_update_rejects_server_fields() -> None:
    with pytest.raises(ValidationError):
        c.TeamMemberUpdate(
            payout_email="a@b.co", email_changed_at="2026-11-04T18:40:00Z"
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        c.TeamMemberUpdate(first_paid_at="2026-11-04T18:40:00Z")  # type: ignore[call-arg]


def test_team_member_create_rejects_nonpositive_rate() -> None:
    with pytest.raises(ValidationError):
        c.TeamMemberCreate(
            name="Omar", payout_email="omar@example.com", hourly_rate="0.00"
        )  # type: ignore[arg-type]


def test_mapping_confirm_rejects_duplicate_targets() -> None:
    with pytest.raises(ValidationError):
        c.TimesheetMappingConfirm(mapping={"Hrs": "hours", "Hours": "hours"})
    ok = c.TimesheetMappingConfirm(mapping={"Employee": "member", "Hrs": "hours"})
    assert ok.mapping["Hrs"] == "hours"


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        ({"mode": "MILESTONE", "milestone_id": "ms_01"}, True),
        ({"mode": "MONTHLY", "period": "2026-11"}, True),
        ({"mode": "MILESTONE"}, False),
        ({"mode": "MONTHLY"}, False),
        ({"mode": "MONTHLY", "period": "2026-13"}, False),
        ({"mode": "MILESTONE", "milestone_id": "ms_01", "period": "2026-11"}, False),
    ],
)
def test_run_create_mode_rules(payload: dict[str, str], valid: bool) -> None:
    if valid:
        c.PayrollRunCreate(**payload)  # type: ignore[arg-type]
    else:
        with pytest.raises(ValidationError):
            c.PayrollRunCreate(**payload)  # type: ignore[arg-type]


def test_send_request_requires_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        c.PayrollSendRequest(confirm=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        c.PayrollSendRequest(confirm=True, idempotency_key="")
    assert (
        c.PayrollSendRequest(confirm=True, idempotency_key="k1").approve_run_flags == []
    )


def test_detective_models_leash_fields_default_none() -> None:
    from pathlib import Path

    base = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "json"
    sim = c.SimulatorPaymentResponse.model_validate(
        json.loads((base / "acme_simulate_response.json").read_text(encoding="utf-8"))
    )
    assert sim.leash is None and sim.payroll_run_id is None
    ledger = json.loads((base / "acme_ledger.json").read_text(encoding="utf-8"))
    rows = ledger["rows"] if isinstance(ledger, dict) else ledger
    assert rows
    for row in rows:
        lr = c.LedgerRow.model_validate(row)
        assert lr.leash_decision is None and lr.remediation_sent_by is None


def test_team_member_email_is_lowercased() -> None:
    raw = " Omar.New@Personal.Example.COM "
    want = "omar.new@personal.example.com"
    created = c.TeamMemberCreate(name="Omar", payout_email=raw, hourly_rate="10.00")  # type: ignore[arg-type]
    assert created.payout_email == want
    assert c.TeamMemberUpdate(payout_email=raw).payout_email == want  # type: ignore[arg-type]


def test_team_member_update_rejects_null_and_empty() -> None:
    with pytest.raises(ValidationError):
        c.TeamMemberUpdate(name=None)
    with pytest.raises(ValidationError):
        c.TeamMemberUpdate()
    assert c.TeamMemberUpdate(active=False).active is False


@pytest.mark.parametrize("bad", [False, 1, "yes"])
def test_send_request_confirm_must_be_literal_true(bad: object) -> None:
    with pytest.raises(ValidationError):
        c.PayrollSendRequest(confirm=bad, idempotency_key="k1")  # type: ignore[arg-type]
    assert c.PayrollSendRequest(confirm=True, idempotency_key="k1").confirm is True


def test_hourly_rate_fits_numeric_8_2() -> None:
    with pytest.raises(ValidationError):
        c.TeamMemberCreate(
            name="Omar", payout_email="omar@example.com", hourly_rate="1234567890.00"
        )  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        c.TeamMemberUpdate(hourly_rate="1234567890.00")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        c.TeamMemberUpdate(name="x" * 121)


def test_mapping_confidence_bounded() -> None:
    with pytest.raises(ValidationError):
        c.ColumnMappingProposal(mapping={"Pay": "rate"}, confidence={"Pay": 7.0})
    ok = c.ColumnMappingProposal(mapping={"Pay": "rate"}, confidence={"Pay": 0.7})
    assert ok.confidence["Pay"] == 0.7


def test_deny_ledger_row_can_be_blocked() -> None:
    from pathlib import Path

    base = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "json"
    ledger = json.loads((base / "acme_ledger.json").read_text(encoding="utf-8"))
    rows = ledger["rows"] if isinstance(ledger, dict) else ledger
    row = {**rows[0], "discrepancy_status": "BLOCKED", "leash_decision": "DENY"}
    lr = c.LedgerRow.model_validate(row)
    assert lr.discrepancy_status == c.DiscrepancyStatus.BLOCKED


_MARGIN_ROW = {
    "milestone_id": "ms_05",
    "milestone_code": "MS-03",
    "cash_in": "0.00",
    "invoiced_open": "0.00",
    "paid_to_team": "0.00",
    "pending_to_team": "0.00",
    "margin": "0.00",
    "margin_pct": "0.00",
}


def test_margin_row_without_run_defaults_to_none_status() -> None:
    assert c.MarginRow.model_validate(_MARGIN_ROW).payroll_status == "NONE"


def test_margin_row_rejects_null_and_unknown_status() -> None:
    for bad in (None, "DONE"):
        with pytest.raises(ValidationError):
            c.MarginRow.model_validate({**_MARGIN_ROW, "payroll_status": bad})
