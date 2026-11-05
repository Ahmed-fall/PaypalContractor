"""Tests for the deterministic explanation templates."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from backend.app.leash.explain import MORE_CHECKS, template_reason
from backend.app.schemas.contracts import GuardAction, LeashDecision

ACTION = GuardAction.SEND_REMEDIATION_INVOICE
DEFAULT = (
    "Leash couldn't confirm this action is safe to run on its own. Review it first."
)


def reason(decision: LeashDecision, ids: list[str], facts: dict[str, Any]) -> str:
    return template_reason(ACTION, decision, ids, facts)


def test_template_known_rules() -> None:
    facts = {"amount": "200.00"}
    assert reason(LeashDecision.ALLOW, ["G-01"], facts) == (
        "$200.00 is within your auto-send limit and the breached clause is quoted."
    )
    assert reason(LeashDecision.ASK, ["G-02"], {"amount": Decimal("250.50")}) == (
        "$250.50 is above your auto-send limit. Review it before sending."
    )
    assert reason(LeashDecision.ASK, ["G-03"], facts) == (
        "This client is marked never_auto_contact. Review before sending."
    )
    assert reason(LeashDecision.ASK, ["G-04"], facts) == (
        "This client already got a correction invoice recently. Review before sending."
    )
    assert reason(LeashDecision.DENY, ["G-05"], facts) == (
        "The invoice line items don't add up to the amount owed, so it can't be sent."
    )
    assert reason(LeashDecision.ASK, ["DEFAULT"], facts) == DEFAULT
    assert reason(LeashDecision.ASK, ["G-99"], facts) == DEFAULT
    assert reason(LeashDecision.ASK, [], facts) == DEFAULT


def test_template_missing_or_invalid_amount() -> None:
    expected = "This amount is above your auto-send limit. Review it before sending."
    for facts in ({}, {"amount": 200.0}, {"amount": "200"}, {"amount": "-1.00"}):
        assert reason(LeashDecision.ASK, ["G-02"], facts) == expected


def test_template_joins_multiple_ids_and_dedupes() -> None:
    text = reason(LeashDecision.ASK, ["G-03", "G-04", "G-03", "X"], {})
    assert text.startswith("This client is marked never_auto_contact. Review before")
    assert text.count("never_auto_contact") == 1


def test_template_max_200_chars_keeps_whole_sentences() -> None:
    facts = {"amount": "200.00", "client": "x" * 10_000}
    ids = ["G-02", "G-03", "G-04", "DEFAULT"]
    text = reason(LeashDecision.ASK, ids, facts)
    assert len(text) <= 200
    # never cut mid-sentence: whole sentences, then a note that more checks apply
    g02 = reason(LeashDecision.ASK, ["G-02"], facts)
    g03 = reason(LeashDecision.ASK, ["G-03"], facts)
    assert text == f"{g02} {g03} {MORE_CHECKS}"
    assert "…" not in text


def test_truncation_without_spaces() -> None:
    from backend.app.leash import explain

    assert explain._truncate("x" * 300) == "x" * 199 + "…"


def test_template_never_raises_on_garbage() -> None:
    assert reason(LeashDecision.ASK, ["G-02"], {"amount": object()}).startswith(
        "This amount"
    )
    assert reason(LeashDecision.ASK, ["G-02"], {}).startswith("This amount")
    assert reason(LeashDecision.ASK, [object()], {}) == DEFAULT  # type: ignore[list-item]
    assert reason(LeashDecision.ASK, ["G-02"], None) == DEFAULT  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "ids", [["G-01"], ["G-02"], ["G-03"], ["G-04"], ["G-05"], ["DEFAULT"]]
)
def test_template_has_no_numbers_not_in_facts(ids: list[str]) -> None:
    facts = {"amount": "200.00", "client": "Acme 42"}
    for digits in re.findall(r"\d+", reason(LeashDecision.ASK, ids, facts)):
        assert digits in str(facts["amount"])


ALLOW_DEFAULT = "Your Leash policy allows this action to run on its own."
DENY_DEFAULT = "Your Leash policy blocks this action."


@pytest.mark.parametrize(
    ("decision", "ids", "facts", "expected"),
    [
        (LeashDecision.ALLOW, ["G-06"], {"amount": "100.00"}, ALLOW_DEFAULT),
        (LeashDecision.ALLOW, ["G-02"], {"amount": "400.00"}, ALLOW_DEFAULT),
        (LeashDecision.DENY, ["MY-RULE"], {"amount": "100.00"}, DENY_DEFAULT),
        (LeashDecision.ASK, ["G-01"], {"amount": "100.00"}, DEFAULT),
        (LeashDecision.DENY, ["G-02"], {"amount": "100.00"}, DENY_DEFAULT),
        (LeashDecision.ASK, ["G-05"], {"amount": "100.00"}, DEFAULT),
        (LeashDecision.ALLOW, [], {}, ALLOW_DEFAULT),
    ],
)
def test_template_never_contradicts_decision(
    decision: LeashDecision,
    ids: list[str],
    facts: dict[str, Any],
    expected: str,
) -> None:
    text = reason(decision, ids, facts)
    assert text == expected
    assert "above your auto-send limit" not in text


PAY = GuardAction.PAY_TEAM_MEMBER
BATCH = GuardAction.SEND_PAYROLL_BATCH
PAYROLL_SENTENCES: list[tuple[GuardAction, LeashDecision, str, str]] = [
    (
        PAY,
        LeashDecision.ASK,
        "PR-01",
        (
            "This team member's payout email changed recently. "
            "Confirm it with them before paying."
        ),
    ),
    (
        PAY,
        LeashDecision.ASK,
        "PR-02",
        (
            "The hours on this line are more than one person can work. "
            "Check the timesheet before paying."
        ),
    ),
    (
        PAY,
        LeashDecision.DENY,
        "PR-02",
        "The hours on this line are zero or negative, so it can't be paid.",
    ),
    (
        PAY,
        LeashDecision.ASK,
        "PR-03",
        (
            "This is the first payment to this team member. "
            "Check their details before paying."
        ),
    ),
    (
        PAY,
        LeashDecision.ASK,
        "PR-04",
        (
            "The rate in the timesheet doesn't match the roster, so the roster rate "
            "is used. Review it before paying."
        ),
    ),
    (
        PAY,
        LeashDecision.DENY,
        "PR-05",
        (
            "This person isn't in the roster or appears more than once, "
            "so they can't be paid."
        ),
    ),
    (
        BATCH,
        LeashDecision.ASK,
        "PR-06",
        (
            "This payroll run is above your margin limit for the cash received. "
            "Review it before sending."
        ),
    ),
    (
        PAY,
        LeashDecision.DENY,
        "PR-07",
        (
            "This team member was already paid for this milestone, "
            "so they can't be paid again."
        ),
    ),
]


@pytest.mark.parametrize(
    ("action", "decision", "rule_id", "expected"), PAYROLL_SENTENCES
)
def test_template_payroll_rules(
    action: GuardAction, decision: LeashDecision, rule_id: str, expected: str
) -> None:
    text = template_reason(action, decision, [rule_id], {"amount": "450.00"})
    assert text == expected
    assert rule_id not in text
    assert not re.search(r"\d", text)


def test_template_pr02_follows_the_decision() -> None:
    ask = template_reason(PAY, LeashDecision.ASK, ["PR-02"], {})
    deny = template_reason(PAY, LeashDecision.DENY, ["PR-02"], {})
    assert "more than one person can work" in ask
    assert "zero or negative" in deny
    assert template_reason(PAY, LeashDecision.ALLOW, ["PR-02"], {}) == ALLOW_DEFAULT


@pytest.mark.parametrize(
    ("decision", "rule_id"),
    [
        (LeashDecision.DENY, "PR-01"),
        (LeashDecision.ALLOW, "PR-03"),
        (LeashDecision.ASK, "PR-05"),
        (LeashDecision.DENY, "PR-06"),
        (LeashDecision.ASK, "PR-07"),
    ],
)
def test_template_payroll_never_contradicts_decision(
    decision: LeashDecision, rule_id: str
) -> None:
    defaults = {
        LeashDecision.ALLOW: ALLOW_DEFAULT,
        LeashDecision.ASK: DEFAULT,
        LeashDecision.DENY: DENY_DEFAULT,
    }
    assert template_reason(PAY, decision, [rule_id], {}) == defaults[decision]


@pytest.mark.parametrize("decision", list(LeashDecision))
def test_template_all_ids_at_once_max_200_chars(decision: LeashDecision) -> None:
    ids = [f"G-0{i}" for i in range(1, 6)] + [f"PR-0{i}" for i in range(1, 8)]
    text = template_reason(PAY, decision, ids, {"amount": "200.00"})
    assert 0 < len(text) <= 200


# --- review fixes (PR #11) ----------------------------------------------------------------

PR02_ASK = "more than one person can work"
PR02_DENY = "zero or negative"
PR02_NEUTRAL = "The hours on this line need checking."


@pytest.mark.parametrize("other", ["PR-05", "PR-07"])
def test_pr02_with_another_deny_rule_never_states_the_wrong_hours_case(
    other: str,
) -> None:
    """check_line uses one id for both PR-02 cases; a DENY may come from PR-05/PR-07
    while PR-02 was the too-many-hours case, so only a neutral sentence is true."""
    text = template_reason(PAY, LeashDecision.DENY, ["PR-02", other], {})
    assert PR02_NEUTRAL in text
    assert PR02_DENY not in text
    assert PR02_ASK not in text


def test_pr02_deny_alone_still_says_zero_or_negative() -> None:
    assert PR02_DENY in template_reason(PAY, LeashDecision.DENY, ["PR-02"], {})


@pytest.mark.parametrize(
    ("action", "decision", "rule_id", "expected"),
    [
        (PAY, LeashDecision.ALLOW, "G-01", ALLOW_DEFAULT),
        (PAY, LeashDecision.ASK, "G-02", DEFAULT),
        (BATCH, LeashDecision.DENY, "PR-07", DENY_DEFAULT),
        (PAY, LeashDecision.ASK, "PR-06", DEFAULT),
        (ACTION, LeashDecision.ASK, "PR-01", DEFAULT),
        (BATCH, LeashDecision.ASK, "PR-03", DEFAULT),
    ],
)
def test_rule_sentences_only_apply_to_their_own_action(
    action: GuardAction, decision: LeashDecision, rule_id: str, expected: str
) -> None:
    """A user rule reusing a G-/PR- id on another action must not borrow its sentence."""
    assert (
        template_reason(action, decision, [rule_id], {"amount": "100.00"}) == expected
    )


def test_combined_payroll_ask_keeps_every_kept_sentence_whole() -> None:
    text = template_reason(PAY, LeashDecision.ASK, ["PR-01", "PR-03", "PR-04"], {})
    assert len(text) <= 200
    assert "…" not in text
    singles = [
        template_reason(PAY, LeashDecision.ASK, [i], {})
        for i in ("PR-01", "PR-03", "PR-04")
    ]
    kept = text.removesuffix(MORE_CHECKS).strip()
    assert kept, text
    # what remains is an exact prefix of whole sentences, in order
    assert " ".join(singles).startswith(kept)
    assert any(kept == " ".join(singles[:n]) for n in range(1, 4))
    if kept != " ".join(singles):
        assert text.endswith(MORE_CHECKS)


def test_more_checks_note_has_no_digits_and_is_short() -> None:
    assert not re.search(r"\d", MORE_CHECKS)
    assert len(MORE_CHECKS) < 40


def test_join_whole_keeps_a_long_first_sentence_without_the_note() -> None:
    from backend.app.leash import explain

    first = ("word " * 38).strip() + "."  # 190 chars: fits alone, not with the note
    assert len(first) <= 200 < len(f"{first} {MORE_CHECKS}")
    assert explain._join_whole([first, "Second sentence."]) == first


def test_join_whole_truncates_a_single_oversized_sentence() -> None:
    from backend.app.leash import explain

    text = explain._join_whole([("word " * 60).strip() + ".", "Next."])
    assert len(text) <= 200
    assert text.endswith("…")


def test_join_whole_of_nothing_is_empty() -> None:
    from backend.app.leash import explain

    assert explain._join_whole([]) == ""
