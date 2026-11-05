"""Pure, synchronous Leash action guard.

AI can raise suspicion; only code can say yes. ``evaluate_rules`` does no I/O and
calls no model. It is fail-closed: a condition is *unknown* when its key is outside
the vocabulary, its fact is missing, or the fact or rule value has the wrong type.
An ALLOW rule with an unknown condition does not match; an ASK or DENY rule with an
unknown condition (and no definitely-false one) matches. Merge rule: DENY > ASK >
ALLOW. If nothing matches the result is ``(ASK, ["DEFAULT"])``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from ..schemas.contracts import GuardAction, LeashDecision, LeashPolicy, LeashRule
from .policy import BUILTIN_RULES

DEFAULT_RULE_ID = "DEFAULT"
PAYROLL_CHECKS_RULE_ID = "PAYROLL-CHECKS"
PAYROLL_ACTIONS: frozenset[GuardAction] = frozenset(
    {GuardAction.PAY_TEAM_MEMBER, GuardAction.SEND_PAYROLL_BATCH}
)

KNOWN_CONDITIONS: frozenset[str] = frozenset(
    {
        "amount_lte",
        "amount_gt",
        "confidence_gte",
        "clause_quote_present",
        "client_tag",
        "invoiced_same_client_within_days",
        "line_items_sum_mismatch",
    }
)

# Policy ALLOW rules never match payroll: only the deterministic payroll checks
# (facts["payroll_check"]) can supply a payroll ALLOW; policy rules can only tighten it.
ALLOW_ENABLED_ACTIONS: frozenset[GuardAction] = frozenset(
    {GuardAction.SEND_REMEDIATION_INVOICE}
)

_MONEY_2DP = re.compile(r"[0-9]+\.[0-9]{2}")
_FLAG = re.compile(r"PR-0[1-7]")
_RANK = {LeashDecision.ALLOW: 0, LeashDecision.ASK: 1, LeashDecision.DENY: 2}


def _money(value: object) -> Decimal | None:
    """A finite Decimal, or a 2-dp string. Floats and anything else are unknown."""
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0 or value.as_tuple().exponent != -2:
            return None
        return value
    if isinstance(value, str) and _MONEY_2DP.fullmatch(value):
        return Decimal(value)
    return None


parse_money = _money
"""Public name for the guard's money rule, shared with the explanation templates."""


def _number(value: object) -> Decimal | None:
    """A finite int/float/Decimal as Decimal. Bools and strings are unknown."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


def _days(value: object) -> int | None:
    """A non-negative int (bools excluded), else unknown."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _check_amount(key: str, rule_value: object, facts: dict[str, Any]) -> bool | None:
    amount = _money(facts.get("amount"))
    limit = _money(rule_value)
    if amount is None or limit is None:
        return None
    return amount <= limit if key == "amount_lte" else amount > limit


def _check_confidence(rule_value: object, facts: dict[str, Any]) -> bool | None:
    confidence = _number(facts.get("extraction_confidence"))
    limit = _number(rule_value)
    if confidence is None or limit is None or not Decimal(0) <= confidence <= 1:
        return None
    return confidence >= limit


def _check_bool(rule_value: object, fact: object) -> bool | None:
    if not isinstance(rule_value, bool) or not isinstance(fact, bool):
        return None
    return fact is rule_value


def _check_tag(rule_value: object, facts: dict[str, Any]) -> bool | None:
    tags = facts.get("client_tags")
    if (
        not isinstance(rule_value, str)
        or not isinstance(tags, list)
        or not all(isinstance(tag, str) for tag in tags)
    ):
        return None
    wanted = rule_value.strip().casefold()
    return wanted in {tag.strip().casefold() for tag in tags}


def _check_recent_invoice(rule_value: object, facts: dict[str, Any]) -> bool | None:
    window = _days(rule_value)
    key = "days_since_last_client_invoice"
    if window is None or key not in facts:
        return None
    since = facts[key]
    if since is None:
        return False  # never invoiced
    days = _days(since)
    if days is None:
        return None
    return days <= window


def _check_mismatch(rule_value: object, facts: dict[str, Any]) -> bool | None:
    flag = facts.get("line_items_match_decimal_total")
    if not isinstance(flag, bool):
        return None
    return _check_bool(rule_value, not flag)


def _condition(key: str, rule_value: object, facts: dict[str, Any]) -> bool | None:
    """Three-valued check: True, False, or None (unknown)."""
    if key in ("amount_lte", "amount_gt"):
        return _check_amount(key, rule_value, facts)
    if key == "confidence_gte":
        return _check_confidence(rule_value, facts)
    if key == "clause_quote_present":
        return _check_bool(rule_value, facts.get("has_clause_quote"))
    if key == "client_tag":
        return _check_tag(rule_value, facts)
    if key == "invoiced_same_client_within_days":
        return _check_recent_invoice(rule_value, facts)
    if key == "line_items_sum_mismatch":
        return _check_mismatch(rule_value, facts)
    return None


def _matches(rule: LeashRule, facts: dict[str, Any]) -> bool:
    results = [_condition(k, v, facts) for k, v in rule.when.items()]
    if False in results:
        return False
    if rule.outcome == LeashDecision.ALLOW:
        if rule.applies_to not in ALLOW_ENABLED_ACTIONS:
            return False
        # defence in depth: an ALLOW rule needs at least one condition, all true
        return bool(results) and None not in results
    return True


def _payroll_check(
    action: GuardAction, facts: dict[str, Any]
) -> tuple[LeashDecision, list[str]] | None:
    """The base decision from ``facts["payroll_check"]``, or None if absent or invalid.

    Valid: a dict with exactly ``decision`` (a LeashDecision or its exact string value)
    and ``flags`` (distinct ``PR-01``..``PR-07`` strings in a list or tuple). ALLOW needs
    no flags; ASK and DENY need at least one.
    """
    if action not in PAYROLL_ACTIONS:
        return None
    check = facts.get("payroll_check")
    if not isinstance(check, dict) or set(check) != {"decision", "flags"}:
        return None
    raw, flags = check["decision"], check["flags"]
    if isinstance(raw, LeashDecision):
        decision = raw
    elif isinstance(raw, str) and raw in {d.value for d in LeashDecision}:
        decision = LeashDecision(raw)
    else:
        return None
    if not isinstance(flags, list | tuple):
        return None
    if not all(isinstance(f, str) and _FLAG.fullmatch(f) for f in flags):
        return None
    if len(set(flags)) != len(flags):
        return None
    if (decision == LeashDecision.ALLOW) != (len(flags) == 0):
        return None
    return decision, list(flags) or [PAYROLL_CHECKS_RULE_ID]


def evaluate_rules(
    policy: LeashPolicy, action: GuardAction, facts: dict[str, Any]
) -> tuple[LeashDecision, list[str]]:
    """Decide ALLOW/ASK/DENY for ``action``. Pure: no I/O, no mutation, no AI."""
    builtins = [r for r in BUILTIN_RULES if r.applies_to == action]
    reserved = {r.id for r in builtins} | {DEFAULT_RULE_ID, PAYROLL_CHECKS_RULE_ID}
    # Built-ins are authoritative: a policy rule reusing a built-in id (or the
    # reserved DEFAULT id) is ignored.
    rules = [r for r in policy.rules if r.applies_to == action and r.id not in reserved]
    rules.extend(builtins)
    matched = [r for r in rules if _matches(r, facts)]
    base = _payroll_check(action, facts)
    if base is None:
        if not matched:
            return LeashDecision.ASK, [DEFAULT_RULE_ID]
        final = max((r.outcome for r in matched), key=_RANK.__getitem__)
    else:
        final = max([base[0], *(r.outcome for r in matched)], key=_RANK.__getitem__)
    ids = list(base[1]) if base is not None and base[0] == final else []
    ids.extend(r.id for r in matched if r.outcome == final)
    return final, list(dict.fromkeys(ids))
