"""Payroll decisions come from the deterministic checks (facts["payroll_check"])."""

from __future__ import annotations

from enum import Enum
from typing import Any

import pytest

from backend.app.leash.explain import template_reason
from backend.app.leash.guard import evaluate_rules
from backend.app.leash.policy import default_policy
from backend.app.schemas.contracts import (
    GuardAction,
    LeashDecision,
    LeashPolicy,
    LeashRule,
)

ALLOW, ASK, DENY = LeashDecision.ALLOW, LeashDecision.ASK, LeashDecision.DENY
LINE = GuardAction.PAY_TEAM_MEMBER
RUN = GuardAction.SEND_PAYROLL_BATCH
BOTH = (LINE, RUN)


def pol(*rules: LeashRule) -> LeashPolicy:
    return LeashPolicy(version=1, rules=list(rules))


def rule(rid: str, when: dict[str, Any], outcome: LeashDecision, action: GuardAction) -> LeashRule:
    return LeashRule(id=rid, applies_to=action, when=when, outcome=outcome, source_text="t")


class LyingStr(str):
    """Real value ASK, but equal to everything and hashing like ALLOW."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash("ALLOW")


class UnhashableStr(str):
    __hash__ = None  # type: ignore[assignment]


class FakeDecision(str, Enum):
    ALLOW = "ALLOW"


class HiddenKeyDict(dict[str, Any]):
    def __iter__(self) -> Any:
        return iter(["decision", "flags"])

    def keys(self) -> Any:
        return ["decision", "flags"]


class FlagTuple(tuple[str, ...]):
    pass


class FlagStr(str):
    pass


def chk(decision: Any, flags: Any) -> dict[str, Any]:
    return {"payroll_check": {"decision": decision, "flags": flags}}


@pytest.mark.parametrize("action", BOTH)
@pytest.mark.parametrize(
    ("decision", "flags", "expected"),
    [
        ("ALLOW", [], (ALLOW, ["PAYROLL-CHECKS"])),
        (ALLOW, (), (ALLOW, ["PAYROLL-CHECKS"])),
        ("ASK", ["PR-03"], (ASK, ["PR-03"])),
        (ASK, ("PR-01", "PR-04"), (ASK, ["PR-01", "PR-04"])),
        ("DENY", ["PR-05"], (DENY, ["PR-05"])),
        (DENY, ["PR-06"], (DENY, ["PR-06"])),
        ("ASK", ["PR-06", "PR-05", "PR-07"], (ASK, ["PR-06", "PR-05", "PR-07"])),
    ],
)
def test_valid_check_supplies_decision(
    action: GuardAction, decision: Any, flags: Any, expected: Any
) -> None:
    assert evaluate_rules(default_policy(), action, chk(decision, flags)) == expected


BAD_CHECKS: list[Any] = [
    None,
    True,
    "ALLOW",
    ["ALLOW", []],
    {},
    {"decision": "ALLOW"},
    {"decision": "ALLOW", "flags": [], "extra": 1},
    {"decision": "allow", "flags": []},
    {"decision": True, "flags": []},
    {"decision": 0, "flags": []},
    {"decision": "MAYBE", "flags": []},
    {"decision": "ALLOW", "flags": ["PR-01"]},
    {"decision": "ASK", "flags": []},
    {"decision": "DENY", "flags": []},
    {"decision": "ASK", "flags": "PR-01"},
    {"decision": "ASK", "flags": {"PR-01"}},
    {"decision": "ASK", "flags": ["PR-08"]},
    {"decision": "ASK", "flags": ["PR-00"]},
    {"decision": "ASK", "flags": ["pr-01"]},
    {"decision": "ASK", "flags": [" PR-01"]},
    {"decision": "ASK", "flags": ["PR-01\n"]},
    {"decision": "ASK", "flags": ["PR-1"]},
    {"decision": "ASK", "flags": [1]},
    {"decision": "ASK", "flags": ["PR-01", "PR-01"]},
    {"decision": "ASK", "flags": ["G-01"]},
    {"decision": LyingStr("ASK"), "flags": []},
    {"decision": FakeDecision.ALLOW, "flags": []},
    HiddenKeyDict(decision="ALLOW", flags=[], extra=1),
    {"decision": "ALLOW", "flags": FlagTuple()},
    {"decision": "ASK", "flags": [UnhashableStr("PR-01")]},
    {"decision": "ASK", "flags": [FlagStr("PR-01")]},
    {"decision": FlagStr("ALLOW"), "flags": []},
]


@pytest.mark.parametrize("action", BOTH)
@pytest.mark.parametrize("bad", BAD_CHECKS)
def test_invalid_check_falls_back_to_default(action: GuardAction, bad: Any) -> None:
    out = evaluate_rules(default_policy(), action, {"payroll_check": bad})
    assert out == (ASK, ["DEFAULT"])


@pytest.mark.parametrize("action", BOTH)
def test_missing_check_is_default(action: GuardAction) -> None:
    assert evaluate_rules(default_policy(), action, {}) == (ASK, ["DEFAULT"])


def test_check_ignored_for_invoice_action() -> None:
    inv = GuardAction.SEND_REMEDIATION_INVOICE
    with_check = evaluate_rules(default_policy(), inv, chk("ALLOW", []))
    assert with_check == evaluate_rules(default_policy(), inv, {})
    assert with_check[0] != ALLOW


@pytest.mark.parametrize("action", BOTH)
def test_owner_ask_rule_tightens_allow_check(action: GuardAction) -> None:
    policy = pol(rule("P-1", {"amount_gt": "100.00"}, ASK, action))
    facts = {**chk("ALLOW", []), "amount": "150.00"}
    assert evaluate_rules(policy, action, facts) == (ASK, ["P-1"])
    facts["amount"] = "50.00"
    assert evaluate_rules(policy, action, facts) == (ALLOW, ["PAYROLL-CHECKS"])


def test_owner_deny_rule_tightens_ask_check() -> None:
    policy = pol(rule("P-1", {}, DENY, LINE), rule("P-2", {}, DENY, LINE))
    out = evaluate_rules(policy, LINE, chk("ASK", ["PR-03"]))
    assert out == (DENY, ["P-1", "P-2"])


def test_equal_strictness_merges_check_ids_first() -> None:
    policy = pol(rule("P-1", {}, ASK, LINE), rule("PR-03", {}, ASK, LINE))
    out = evaluate_rules(policy, LINE, chk("ASK", ["PR-03", "PR-01"]))
    assert out == (ASK, ["PR-03", "PR-01", "P-1"])


def test_deny_check_stays_deny_against_allow_policy_rule() -> None:
    policy = pol(rule("P-1", {"amount_lte": "5000.00"}, ALLOW, LINE))
    out = evaluate_rules(policy, LINE, {**chk("DENY", ["PR-05"]), "amount": "1.00"})
    assert out == (DENY, ["PR-05"])


def test_allow_policy_rule_cannot_loosen_ask_check() -> None:
    policy = pol(rule("P-1", {"amount_lte": "5000.00"}, ALLOW, LINE))
    out = evaluate_rules(policy, LINE, {**chk("ASK", ["PR-03"]), "amount": "1.00"})
    assert out == (ASK, ["PR-03"])


def test_run_action_with_pr06() -> None:
    assert evaluate_rules(default_policy(), RUN, chk("ASK", ["PR-06"])) == (
        ASK,
        ["PR-06"],
    )
    assert evaluate_rules(default_policy(), RUN, chk("ASK", ["PR-05"])) == (
        ASK,
        ["PR-05"],
    )


def test_reserved_payroll_checks_id_in_policy_is_ignored() -> None:
    policy = pol(rule("PAYROLL-CHECKS", {}, DENY, LINE))
    assert evaluate_rules(policy, LINE, chk("ALLOW", [])) == (
        ALLOW,
        ["PAYROLL-CHECKS"],
    )
    assert evaluate_rules(policy, LINE, {}) == (ASK, ["DEFAULT"])


def test_policy_ask_without_check_unchanged() -> None:
    policy = pol(rule("P-1", {}, ASK, LINE))
    assert evaluate_rules(policy, LINE, {}) == (ASK, ["DEFAULT", "P-1"])
    bad = {"payroll_check": {"decision": "ALLOW", "flags": ["PR-01"]}}
    assert evaluate_rules(policy, RUN, bad) == (ASK, ["DEFAULT"])  # no RUN rule
    deny = pol(rule("P-2", {}, DENY, LINE))
    assert evaluate_rules(deny, LINE, {}) == (DENY, ["P-2"])


def test_does_not_mutate_facts() -> None:
    facts = chk("ASK", ["PR-03"])
    evaluate_rules(default_policy(), LINE, facts)
    assert facts == chk("ASK", ["PR-03"])


@pytest.mark.parametrize("action", BOTH)
def test_payroll_checks_allow_template(action: GuardAction) -> None:
    out = template_reason(action, ALLOW, ["PAYROLL-CHECKS"], {})
    assert out == "All payroll checks passed."


def test_payroll_checks_template_not_for_invoice_or_other_decision() -> None:
    inv = GuardAction.SEND_REMEDIATION_INVOICE
    text = "All payroll checks passed."
    assert template_reason(inv, ALLOW, ["PAYROLL-CHECKS"], {}) != text
    assert template_reason(LINE, ASK, ["PAYROLL-CHECKS"], {}) != text
