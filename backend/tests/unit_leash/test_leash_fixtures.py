"""Each shared/fixtures/leash artifact must validate against contracts.py and keep its money math."""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from backend.app.schemas import contracts as c

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "leash"

CASES: dict[str, type] = {
    "policy_compile_response.json": c.PolicyCompileResponse,
    "active_policy.json": c.ActiveLeashPolicy,
    "decisions.json": c.DecisionLogResponse,
    "team_member_omar.json": c.TeamMemberOut,
    "timesheet_mapping_required.json": c.TimesheetMappingRequired,
    "timesheet_parsed.json": c.TimesheetParsed,
    "payroll_run_awaiting_approval.json": c.PayrollRunOut,
    "payroll_run_after_exclude.json": c.PayrollRunOut,
    "payroll_send_response.json": c.PayrollSendResponse,
    "ledger_margin.json": c.MarginResponse,
    "simulate_response_leash.json": c.SimulatorPaymentResponse,
}

MONEY_KEYS = frozenset(
    {
        "total",
        "cash_in",
        "cash_used_pct",
        "gross",
        "hours",
        "rate",
        "hourly_rate",
        "amount",
        "paid_to_team",
        "margin",
        "margin_pct",
        "invoiced_open",
        "pending_to_team",
        "payroll_margin_limit_pct",
    }
)
CENT = Decimal("0.01")


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _money_values(node: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in MONEY_KEYS and value is not None:
                found.append((key, value))
            found.extend(_money_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_money_values(item))
    return found


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    return (part / whole * 100).quantize(CENT, ROUND_HALF_UP)


def _run(name: str) -> c.PayrollRunOut:
    return c.PayrollRunOut.model_validate(_load(name))


@pytest.mark.parametrize("filename,model", CASES.items())
def test_fixture_conforms_to_schema(filename: str, model: type) -> None:
    payload = _load(filename)
    reencoded = json.loads(model.model_validate(payload).model_dump_json())
    for key, value in _money_values(reencoded):
        assert isinstance(value, str), f"{filename}: {key} must serialize as a string"


def test_every_fixture_file_is_covered() -> None:
    assert {p.name for p in FIXTURES.glob("*.json")} == set(CASES)


@pytest.mark.parametrize(
    "name", ["payroll_run_awaiting_approval.json", "payroll_run_after_exclude.json"]
)
def test_line_gross_is_hours_times_rate(name: str) -> None:
    for line in _run(name).lines:
        assert line.gross == (line.hours * line.rate).quantize(CENT, ROUND_HALF_UP)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("payroll_run_awaiting_approval.json", Decimal("720.00")),
        ("payroll_run_after_exclude.json", Decimal("420.00")),
    ],
)
def test_run_total_counts_only_payable_lines(name: str, expected: Decimal) -> None:
    run = _run(name)
    payable = sum(
        (
            ln.gross
            for ln in run.lines
            if ln.owner_action != "EXCLUDED" and ln.decision != c.LeashDecision.DENY
        ),
        Decimal(0),
    )
    assert run.total == payable == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("payroll_run_awaiting_approval.json", Decimal("84.71")),
        ("payroll_run_after_exclude.json", Decimal("49.41")),
    ],
)
def test_cash_used_pct_matches_total(name: str, expected: Decimal) -> None:
    run = _run(name)
    assert run.cash_used_pct == _pct(run.total, run.cash_in) == expected


def test_demo_run_flags_exactly_omar_and_run() -> None:
    run = _run("payroll_run_awaiting_approval.json")
    assert run.run_flags == ["PR-06"]
    assert run.run_decision == c.LeashDecision.ASK
    flagged = [ln for ln in run.lines if ln.flags]
    assert [(ln.member_name, ln.flags) for ln in flagged] == [("Omar", ["PR-01"])]
    for ln in run.lines:
        if ln.member_name != "Omar":
            assert ln.decision == c.LeashDecision.ALLOW


def test_after_exclude_keeps_omar_flagged_but_run_clear() -> None:
    run = _run("payroll_run_after_exclude.json")
    omar = run.lines[0]
    assert omar.owner_action == "EXCLUDED"
    assert omar.decision == c.LeashDecision.ASK
    assert omar.flags == ["PR-01"]
    assert run.run_flags == []
    assert run.run_decision == c.LeashDecision.ALLOW


def test_send_matches_checked_run() -> None:
    send = c.PayrollSendResponse.model_validate(_load("payroll_send_response.json"))
    checked = _run("payroll_run_after_exclude.json")
    assert send.total == checked.total
    assert send.lines_sent == 2
    assert send.lines_excluded == 1
    assert send.sender_batch_id == "fd-ms01-prn_0001"


def test_margin_math() -> None:
    margin = c.MarginResponse.model_validate(_load("ledger_margin.json"))
    for row in margin.rows:
        assert row.margin == row.cash_in - row.paid_to_team
        assert row.margin_pct == _pct(row.margin, row.cash_in)
    assert margin.rows[0].margin_pct == Decimal("50.59")
    assert margin.summary.cash_in == sum((r.cash_in for r in margin.rows), Decimal(0))
    assert margin.summary.paid_to_team == sum(
        (r.paid_to_team for r in margin.rows), Decimal(0)
    )
    assert margin.summary.margin == sum((r.margin for r in margin.rows), Decimal(0))


def test_active_policy_matches_compiled_draft() -> None:
    draft = c.PolicyCompileResponse.model_validate(
        _load("policy_compile_response.json")
    )
    active = c.ActiveLeashPolicy.model_validate(_load("active_policy.json"))
    assert active.model_dump(exclude={"activated_at"}) == draft.policy.model_dump()


def test_simulate_leash_allow_is_within_g01() -> None:
    sim = c.SimulatorPaymentResponse.model_validate(
        _load("simulate_response_leash.json")
    )
    policy = c.ActiveLeashPolicy.model_validate(_load("active_policy.json"))
    g01 = next(r for r in policy.rules if r.id == "G-01")
    threshold = Decimal(str(g01.when["amount_lte"]))
    assert sim.leash is not None
    assert sim.leash.decision == c.LeashDecision.ALLOW
    assert sim.leash.rule_ids == ["G-01"]
    assert sim.evaluation.total_remediation_amount <= threshold
    assert sim.payroll_run_id == "prn_0001"


def test_simulate_leash_allow_marks_remediated() -> None:
    sim = c.SimulatorPaymentResponse.model_validate(
        _load("simulate_response_leash.json")
    )
    assert sim.leash is not None
    assert sim.leash.decision == c.LeashDecision.ALLOW
    assert sim.evaluation.discrepancy_status == c.DiscrepancyStatus.REMEDIATED
