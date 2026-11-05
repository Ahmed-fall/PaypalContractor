"""Deterministic decision explanations.

Used by ``decide()`` when there is no explainer or it fails. Sentences cover the
invoice checks G-01…G-05 and the payroll checks PR-01…PR-07, are keyed by rule id
and decision, are used only when they match the decision, never print a rule id,
and only show a number when ``amount`` passes the guard's own money rule. The text is at most 200 characters and this module never raises.
"""

from __future__ import annotations

from typing import Any

from ..schemas.contracts import GuardAction, LeashDecision
from .guard import parse_money

MAX_REASON_LEN = 200
_ELLIPSIS = "…"
# Appended when whole sentences no longer fit: never cut a reason mid-sentence.
MORE_CHECKS = "Other checks also apply."

_DEFAULT = (
    "Leash couldn't confirm this action is safe to run on its own. Review it first."
)
_DEFAULTS: dict[LeashDecision, str] = {
    LeashDecision.ALLOW: "Your Leash policy allows this action to run on its own.",
    LeashDecision.ASK: _DEFAULT,
    LeashDecision.DENY: "Your Leash policy blocks this action.",
}
# rule id -> {decision the sentence is true for: sentence}. Most rules have one
# outcome; PR-02 has two (impossible hours ASK, zero/negative hours DENY).
_SENTENCES: dict[str, dict[LeashDecision, str]] = {
    "G-01": {
        LeashDecision.ALLOW: (
            "{amt} is within your auto-send limit and the breached clause is quoted."
        ),
    },
    "G-02": {
        LeashDecision.ASK: (
            "{amt} is above your auto-send limit. Review it before sending."
        ),
    },
    "G-03": {
        LeashDecision.ASK: (
            "This client is marked never_auto_contact. Review before sending."
        ),
    },
    "G-04": {
        LeashDecision.ASK: (
            "This client already got a correction invoice recently. "
            "Review before sending."
        ),
    },
    "G-05": {
        LeashDecision.DENY: (
            "The invoice line items don't add up to the amount owed, "
            "so it can't be sent."
        ),
    },
    "PR-01": {
        LeashDecision.ASK: (
            "This team member's payout email changed recently. "
            "Confirm it with them before paying."
        ),
    },
    "PR-02": {
        LeashDecision.ASK: (
            "The hours on this line are more than one person can work. "
            "Check the timesheet before paying."
        ),
        LeashDecision.DENY: (
            "The hours on this line are zero or negative, so it can't be paid."
        ),
    },
    "PR-03": {
        LeashDecision.ASK: (
            "This is the first payment to this team member. "
            "Check their details before paying."
        ),
    },
    "PR-04": {
        LeashDecision.ASK: (
            "The rate in the timesheet doesn't match the roster, so the roster rate "
            "is used. Review it before paying."
        ),
    },
    "PR-05": {
        LeashDecision.DENY: (
            "This person isn't in the roster or appears more than once, "
            "so they can't be paid."
        ),
    },
    "PR-06": {
        LeashDecision.ASK: (
            "This payroll run is above your margin limit for the cash received. "
            "Review it before sending."
        ),
    },
    "PR-07": {
        LeashDecision.DENY: (
            "This team member was already paid for this milestone, "
            "so they can't be paid again."
        ),
    },
}


_INVOICE = frozenset({GuardAction.SEND_REMEDIATION_INVOICE})
_LINE = frozenset({GuardAction.PAY_TEAM_MEMBER})
_RUN = frozenset({GuardAction.SEND_PAYROLL_BATCH})
# A rule's sentence describes one action; an owner rule reusing the id on another
# action gets the decision default instead of borrowing a wrong explanation.
_RULE_ACTIONS: dict[str, frozenset[GuardAction]] = {
    **{f"G-0{i}": _INVOICE for i in range(1, 6)},
    **{f"PR-0{i}": _LINE for i in (1, 2, 3, 4, 5, 7)},
    "PR-06": _RUN,
}
# check_line uses one id (PR-02) for both hours cases and a line's decision is the
# strictest flag, so DENY + PR-02 + another deny check cannot tell which case fired.
_OTHER_DENY_IDS = frozenset({"PR-05", "PR-07"})
_PR02_UNKNOWN_CASE = "The hours on this line need checking."


def _amount_text(facts: dict[str, Any]) -> str:
    amount = parse_money(facts.get("amount"))
    return "This amount" if amount is None else f"${amount}"


def _sentence_for(
    rule_id: str, action: GuardAction, decision: LeashDecision, rule_ids: list[str]
) -> str | None:
    if action not in _RULE_ACTIONS.get(rule_id, frozenset()):
        return None
    if (
        rule_id == "PR-02"
        and decision == LeashDecision.DENY
        and _OTHER_DENY_IDS.intersection(rule_ids)
    ):
        return _PR02_UNKNOWN_CASE
    return _SENTENCES.get(rule_id, {}).get(decision)


def _join_whole(sentences: list[str]) -> str:
    """Keep whole sentences that fit, then say more checks apply (<= 200 chars)."""
    full = " ".join(sentences)
    if len(full) <= MAX_REASON_LEN:
        return full
    # the most leading sentences that still fit together with the note
    fits = [
        n
        for n in range(1, len(sentences))
        if len(" ".join([*sentences[:n], MORE_CHECKS])) <= MAX_REASON_LEN
    ]
    if not fits:  # the first sentence alone leaves no room for the note
        return _truncate(sentences[0])
    return " ".join([*sentences[: max(fits)], MORE_CHECKS])


def _truncate(text: str) -> str:
    if len(text) <= MAX_REASON_LEN:
        return text
    space = text.rfind(" ", 0, MAX_REASON_LEN)
    head = text[:space] if space > 0 else text[: MAX_REASON_LEN - 1]
    return head.rstrip() + _ELLIPSIS


def template_reason(
    action: GuardAction,
    decision: LeashDecision,
    rule_ids: list[str],
    facts: dict[str, Any],
) -> str:
    """Return a fixed-copy reason (<= 200 chars). Never raises."""
    try:
        amt = _amount_text(facts)
        sentences: list[str] = []
        for rule_id in rule_ids or ["DEFAULT"]:
            # a rule's sentence is used only for its own action and when it agrees
            # with the decision
            template = _sentence_for(rule_id, action, decision, rule_ids)
            if template is not None:
                sentence = template.format(amt=amt)
            else:
                sentence = _DEFAULTS.get(decision, _DEFAULT)
            if sentence not in sentences:
                sentences.append(sentence)
        return _join_whole(sentences)
    except Exception:  # noqa: BLE001 - the fallback itself must never fail
        return _DEFAULTS.get(decision, _DEFAULT)
