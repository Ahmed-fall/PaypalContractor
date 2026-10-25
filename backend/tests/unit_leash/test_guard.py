"""TST-13: table tests for the pure Leash action guard (evaluate_rules)."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from backend.app.leash.guard import (
    DEFAULT_RULE_ID,
    KNOWN_CONDITIONS,
    evaluate_rules,
)
from backend.app.leash.policy import BUILTIN_RULES, default_policy
from backend.app.schemas.contracts import (
    GuardAction,
    LeashDecision,
    LeashPolicy,
    LeashRule,
)

ALLOW = LeashDecision.ALLOW
ASK = LeashDecision.ASK
DENY = LeashDecision.DENY
INVOICE = GuardAction.SEND_REMEDIATION_INVOICE

BASE: dict[str, Any] = {
    "amount": "200.00",
    "extraction_confidence": 0.98,
    "has_clause_quote": True,
    "client_tags": [],
    "days_since_last_client_invoice": None,
    "line_items_match_decimal_total": True,
    "client": "Acme Corp",
}


def facts(**overrides: Any) -> dict[str, Any]:
    out = copy.deepcopy(BASE)
    out.update(overrides)
    return out


def without(key: str) -> dict[str, Any]:
    out = copy.deepcopy(BASE)
    del out[key]
    return out


def rule(
    rule_id: str,
    when: dict[str, Any],
    outcome: LeashDecision,
    action: GuardAction = INVOICE,
) -> LeashRule:
    return LeashRule(
        id=rule_id, applies_to=action, when=when, outcome=outcome, source_text="t"
    )


def policy_of(*rules: LeashRule) -> LeashPolicy:
    return LeashPolicy(version=1, rules=list(rules))


def no_g05_policy() -> LeashPolicy:
    base = default_policy()
    return policy_of(*[r for r in base.rules if r.id != "G-05"])


G01 = ["G-01"]
DEFAULT = [DEFAULT_RULE_ID]

# (policy | None for default, action, facts, expected decision, expected ids)
CASES = [
    pytest.param(None, INVOICE, facts(), ALLOW, G01, id="base-allow"),
    pytest.param(
        None, INVOICE, facts(amount="300.00"), ASK, ["G-02"], id="over-250-ask"
    ),
    pytest.param(
        None,
        INVOICE,
        facts(amount="120.00", client_tags=["never_auto_contact"]),
        ASK,
        ["G-03"],
        id="never-auto-contact-ask",
    ),
    pytest.param(None, INVOICE, facts(amount="250.00"), ALLOW, G01, id="amount-250"),
    pytest.param(
        None, INVOICE, facts(amount="250.01"), ASK, ["G-02"], id="amount-250-01"
    ),
    pytest.param(
        None, INVOICE, facts(extraction_confidence=0.90), ALLOW, G01, id="conf-0.90"
    ),
    pytest.param(
        None,
        INVOICE,
        facts(extraction_confidence=0.89),
        ASK,
        DEFAULT,
        id="conf-0.89",
    ),
    pytest.param(
        None, INVOICE, facts(has_clause_quote=False), ASK, DEFAULT, id="no-quote"
    ),
    pytest.param(
        None,
        INVOICE,
        facts(days_since_last_client_invoice=3),
        ASK,
        ["G-04"],
        id="days-3",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(days_since_last_client_invoice=7),
        ASK,
        ["G-04"],
        id="days-7",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(days_since_last_client_invoice=8),
        ALLOW,
        G01,
        id="days-8",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(days_since_last_client_invoice=None),
        ALLOW,
        G01,
        id="days-none-never-invoiced",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(days_since_last_client_invoice=0),
        ASK,
        ["G-04"],
        id="days-0",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(line_items_match_decimal_total=False),
        DENY,
        ["G-05"],
        id="mismatch-deny",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(amount="300.00", line_items_match_decimal_total=False),
        DENY,
        ["G-05"],
        id="mismatch-beats-ask",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(
            amount="300.00",
            client_tags=["never_auto_contact"],
            days_since_last_client_invoice=2,
        ),
        ASK,
        ["G-02", "G-03", "G-04"],
        id="ask-merge-policy-order",
    ),
    # fail closed: each evaluated fact missing
    pytest.param(None, INVOICE, without("amount"), ASK, ["G-02"], id="missing-amount"),
    pytest.param(
        None,
        INVOICE,
        without("extraction_confidence"),
        ASK,
        DEFAULT,
        id="missing-confidence",
    ),
    pytest.param(
        None, INVOICE, without("has_clause_quote"), ASK, DEFAULT, id="missing-quote"
    ),
    pytest.param(
        None, INVOICE, without("client_tags"), ASK, ["G-03"], id="missing-tags"
    ),
    pytest.param(
        None,
        INVOICE,
        without("days_since_last_client_invoice"),
        ASK,
        ["G-04"],
        id="missing-days",
    ),
    pytest.param(
        None,
        INVOICE,
        without("line_items_match_decimal_total"),
        DENY,
        ["G-05"],
        id="missing-line-items-flag",
    ),
    # wrong types
    pytest.param(None, INVOICE, facts(amount=200.0), ASK, ["G-02"], id="amount-float"),
    pytest.param(
        None, INVOICE, facts(amount="200"), ASK, ["G-02"], id="amount-not-2dp"
    ),
    pytest.param(
        None, INVOICE, facts(amount="abc"), ASK, ["G-02"], id="amount-garbage"
    ),
    pytest.param(
        None,
        INVOICE,
        facts(extraction_confidence=True),
        ASK,
        DEFAULT,
        id="confidence-bool",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(days_since_last_client_invoice=True),
        ASK,
        ["G-04"],
        id="days-bool",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(client_tags="never_auto_contact"),
        ASK,
        ["G-03"],
        id="tags-str",
    ),
    pytest.param(
        None, INVOICE, facts(has_clause_quote="yes"), ASK, DEFAULT, id="quote-str"
    ),
    # policy variants
    pytest.param(
        no_g05_policy(),
        INVOICE,
        facts(line_items_match_decimal_total=False),
        DENY,
        ["G-05"],
        id="builtin-g05-without-policy-rule",
    ),
    pytest.param(
        policy_of(rule("X-1", {"amount_le": "250.00"}, ALLOW)),
        INVOICE,
        facts(),
        ASK,
        DEFAULT,
        id="unknown-when-key-allow-never-matches",
    ),
    pytest.param(policy_of(), INVOICE, facts(), ASK, DEFAULT, id="empty-rules-policy"),
    pytest.param(
        None,
        GuardAction.PAY_TEAM_MEMBER,
        facts(),
        ASK,
        DEFAULT,
        id="pay-team-member-default",
    ),
    pytest.param(
        None,
        GuardAction.SEND_PAYROLL_BATCH,
        facts(),
        ASK,
        DEFAULT,
        id="send-payroll-batch-default",
    ),
    # Decimal values
    pytest.param(
        None, INVOICE, facts(amount=Decimal("200.00")), ALLOW, G01, id="amount-decimal"
    ),
    pytest.param(
        None,
        INVOICE,
        facts(amount=Decimal("300.00")),
        ASK,
        ["G-02"],
        id="amount-decimal-over",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(extraction_confidence=Decimal("0.90")),
        ALLOW,
        G01,
        id="confidence-decimal",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(line_items_match_decimal_total="no"),
        DENY,
        ["G-05"],
        id="mismatch-flag-str-unknown-denies",
    ),
    pytest.param(
        policy_of(rule("A-1", {}, ASK), rule("G-01", {"amount_lte": "250.00"}, ALLOW)),
        INVOICE,
        facts(),
        ASK,
        ["A-1"],
        id="ask-rule-with-no-conditions-matches",
    ),
    pytest.param(
        None,
        INVOICE,
        facts(amount="0.00"),
        ALLOW,
        G01,
        id="amount-zero",
    ),
]


@pytest.mark.parametrize(("policy", "action", "fact_set", "decision", "ids"), CASES)
def test_guard_table(
    policy: LeashPolicy | None,
    action: GuardAction,
    fact_set: dict[str, Any],
    decision: LeashDecision,
    ids: list[str],
) -> None:
    result = evaluate_rules(policy or default_policy(), action, fact_set)
    assert result == (decision, ids)


def test_guard_table_has_40_cases() -> None:
    assert len(CASES) == 40


def test_evaluate_rules_is_pure() -> None:
    policy = default_policy()
    fact_set = facts(amount="300.00", client_tags=["never_auto_contact"])
    policy_before = policy.model_copy(deep=True)
    facts_before = copy.deepcopy(fact_set)
    first = evaluate_rules(policy, INVOICE, fact_set)
    second = evaluate_rules(policy, INVOICE, fact_set)
    assert first == second
    assert policy == policy_before
    assert fact_set == facts_before


def test_default_policy_matches_a22() -> None:
    policy = default_policy()
    assert policy.version == 1
    assert policy.payroll_margin_limit_pct == Decimal(80)
    assert policy.email_change_window_days == 7
    assert [(r.id, r.when, r.outcome) for r in policy.rules] == [
        (
            "G-01",
            {
                "amount_lte": "250.00",
                "confidence_gte": 0.9,
                "clause_quote_present": True,
            },
            ALLOW,
        ),
        ("G-02", {"amount_gt": "250.00"}, ASK),
        ("G-03", {"client_tag": "never_auto_contact"}, ASK),
        ("G-04", {"invoiced_same_client_within_days": 7}, ASK),
        ("G-05", {"line_items_sum_mismatch": True}, DENY),
    ]
    assert all(r.applies_to == INVOICE for r in policy.rules)
    assert policy.rules[-1].source_text == "(built in)"
    assert all(
        r.source_text != "(built in)" and r.source_text for r in policy.rules[:4]
    )
    assert [r.id for r in BUILTIN_RULES] == ["G-05"]


def test_known_conditions_vocabulary() -> None:
    assert KNOWN_CONDITIONS == {
        "amount_lte",
        "amount_gt",
        "confidence_gte",
        "clause_quote_present",
        "client_tag",
        "invoiced_same_client_within_days",
        "line_items_sum_mismatch",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"amount": 200.0},
        {"amount": "200"},
        {"amount": "abc"},
        {"amount": Decimal("NaN")},
        {"amount": Decimal("Infinity")},
        {"amount": Decimal("-500.00")},
        {"amount": Decimal(200)},
        {"amount": Decimal("200.001")},
        {"extraction_confidence": True},
        {"extraction_confidence": "0.98"},
        {"extraction_confidence": float("nan")},
        {"extraction_confidence": 1.5},
        {"extraction_confidence": -0.1},
        {"days_since_last_client_invoice": True},
        {"days_since_last_client_invoice": "3"},
        {"days_since_last_client_invoice": -1},
        {"days_since_last_client_invoice": 2.5},
        {"client_tags": "never_auto_contact"},
        {"client_tags": None},
        {"client_tags": [1, 2]},
        {"has_clause_quote": "yes"},
        {"has_clause_quote": 1},
        {"has_clause_quote": None},
        {"line_items_match_decimal_total": "no"},
        {"line_items_match_decimal_total": 0},
    ],
)
def test_wrong_typed_facts_never_allow(bad: dict[str, Any]) -> None:
    decision, ids = evaluate_rules(default_policy(), INVOICE, facts(**bad))
    assert decision != ALLOW
    assert ids


@pytest.mark.parametrize(
    "when",
    [
        {"amount_lte": 250},
        {"amount_gt": "abc"},
        {"confidence_gte": "0.9"},
        {"confidence_gte": True},
        {"clause_quote_present": 1},
        {"client_tag": 5},
        {"invoiced_same_client_within_days": "7"},
        {"invoiced_same_client_within_days": True},
        {"line_items_sum_mismatch": "false"},
    ],
)
def test_unusable_rule_values_make_allow_rule_not_match(when: dict[str, Any]) -> None:
    raw = LeashRule.model_construct(
        id="A-1",
        applies_to=INVOICE,
        when=dict(when),
        outcome=ALLOW,
        source_text="t",
    )  # bypasses the schema validator to exercise the guard's own fail-closed path
    pol = LeashPolicy(version=1, rules=[raw])
    assert evaluate_rules(pol, INVOICE, facts()) == (ASK, DEFAULT)


def test_unusable_rule_values_make_ask_rule_match() -> None:
    pol = policy_of(rule("A-1", {"client_tag": 5}, ASK))
    assert evaluate_rules(pol, INVOICE, facts()) == (ASK, ["A-1"])


def test_definite_false_beats_unknown_in_ask_rule() -> None:
    pol = policy_of(rule("A-1", {"amount_gt": "250.00", "client_tag": 5}, ASK))
    assert evaluate_rules(pol, INVOICE, facts()) == (ASK, DEFAULT)


def test_rules_for_other_actions_are_ignored() -> None:
    pol = policy_of(
        rule("P-1", {"amount_lte": "250.00"}, ASK, GuardAction.PAY_TEAM_MEMBER),
        rule("A-1", {"amount_lte": "100.00"}, ALLOW),
    )
    assert evaluate_rules(pol, INVOICE, facts()) == (ASK, DEFAULT)
    assert evaluate_rules(pol, GuardAction.PAY_TEAM_MEMBER, facts()) == (ASK, ["P-1"])


def test_policy_g05_is_not_duplicated() -> None:
    result = evaluate_rules(
        default_policy(),
        INVOICE,
        facts(amount="300.00", line_items_match_decimal_total=False),
    )
    assert result == (DENY, ["G-05"])


def test_builtin_not_added_for_other_actions() -> None:
    result = evaluate_rules(
        no_g05_policy(),
        GuardAction.PAY_TEAM_MEMBER,
        facts(line_items_match_decimal_total=False),
    )
    assert result == (ASK, DEFAULT)


def _colliding_g05(when: dict[str, Any], outcome: LeashDecision) -> LeashRule:
    # model_construct bypasses the validator so even an empty-when ASK is testable
    return LeashRule.model_construct(
        id="G-05",
        applies_to=INVOICE,
        when=when,
        outcome=outcome,
        source_text="evil",
    )


def _policy_with_g05(replacement: LeashRule) -> LeashPolicy:
    rules = [replacement if r.id == "G-05" else r for r in default_policy().rules]
    return LeashPolicy(version=1, rules=rules)


COLLIDING_G05 = [
    pytest.param({"line_items_sum_mismatch": False}, ASK, id="ask-never-fires"),
    pytest.param({"line_items_sum_mismatch": True}, ALLOW, id="allow-on-mismatch"),
    pytest.param({}, ASK, id="ask-empty-when"),
]


@pytest.mark.parametrize(("when", "outcome"), COLLIDING_G05)
def test_policy_cannot_override_builtin_g05(
    when: dict[str, Any], outcome: LeashDecision
) -> None:
    pol = _policy_with_g05(_colliding_g05(when, outcome))
    result = evaluate_rules(pol, INVOICE, facts(line_items_match_decimal_total=False))
    assert result == (DENY, ["G-05"])


@pytest.mark.parametrize(("when", "outcome"), COLLIDING_G05)
def test_colliding_g05_is_ignored_for_clean_facts(
    when: dict[str, Any], outcome: LeashDecision
) -> None:
    pol = _policy_with_g05(_colliding_g05(when, outcome))
    assert evaluate_rules(pol, INVOICE, facts()) == (ALLOW, G01)


def test_rule_ids_are_deduplicated_in_order() -> None:
    base = default_policy()
    g02 = next(r for r in base.rules if r.id == "G-02")
    pol = LeashPolicy(version=1, rules=[*base.rules, g02.model_copy(deep=True)])
    assert evaluate_rules(pol, INVOICE, facts(amount="300.00")) == (ASK, ["G-02"])


def test_allow_rule_with_empty_when_never_matches() -> None:
    sneaky = LeashRule.model_construct(
        id="Z-1", applies_to=INVOICE, when={}, outcome=ALLOW, source_text="t"
    )
    # the ALLOW-needs-a-condition validator re-runs on construction, so skip it
    pol = LeashPolicy.model_construct(version=1, rules=[sneaky])
    result = evaluate_rules(
        pol, INVOICE, facts(amount="99999.00", extraction_confidence=0.1)
    )
    assert result == (ASK, DEFAULT)


@pytest.mark.parametrize("tags", [["Never_Auto_Contact"], [" never_auto_contact "]])
def test_client_tag_is_case_and_space_insensitive(tags: list[str]) -> None:
    assert evaluate_rules(default_policy(), INVOICE, facts(client_tags=tags)) == (
        ASK,
        ["G-03"],
    )


def test_client_tag_with_non_string_entry_fails_closed() -> None:
    decision, _ = evaluate_rules(
        default_policy(), INVOICE, facts(client_tags=["vip", 5])
    )
    assert decision != ALLOW


def test_payroll_actions_never_match_allow_rules() -> None:
    for action in (GuardAction.PAY_TEAM_MEMBER, GuardAction.SEND_PAYROLL_BATCH):
        policy = policy_of(rule("P-1", {"amount_lte": "5000.00"}, ALLOW, action))
        assert evaluate_rules(policy, action, facts(amount="100.00")) == (ASK, DEFAULT)


def test_reserved_default_id_in_policy_is_ignored() -> None:
    policy = default_policy()
    policy.rules.append(rule(DEFAULT_RULE_ID, {"amount_gt": "0.00"}, ASK))
    assert evaluate_rules(policy, INVOICE, facts()) == (ALLOW, G01)
