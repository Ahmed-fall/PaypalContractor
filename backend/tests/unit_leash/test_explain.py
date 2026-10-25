"""Tests for the deterministic explanation templates."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from backend.app.leash.explain import template_reason
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


def test_template_max_200_chars() -> None:
    facts = {"amount": "200.00", "client": "x" * 10_000}
    ids = ["G-02", "G-03", "G-04", "DEFAULT"]
    text = reason(LeashDecision.ASK, ids, facts)
    assert len(text) <= 200
    assert text.endswith("…")
    assert not text[:-1].endswith(" ")
    # cut on a word boundary: the kept text is a prefix made of whole words
    full = " ".join(
        reason(LeashDecision.ASK, [i], facts) for i in ["G-02", "G-03", "G-04"]
    )
    assert full.startswith(text[:-1])
    assert full[len(text) - 1] == " "


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
