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
    [250.0, 250, "abc", "250.001", True, "NaN", "Infinity", "-1.00"],
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
