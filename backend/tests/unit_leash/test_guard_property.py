"""TST-14: AI never yields Allow. The explainer cannot influence the decision."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from backend.app.leash.guard import evaluate_rules
from backend.app.leash.policy import default_policy
from backend.app.leash.service import (
    InMemoryDecisionLog,
    LeashService,
    StaticPolicyProvider,
)
from backend.app.schemas.contracts import GuardAction, LeashDecision

INVOICE = GuardAction.SEND_REMEDIATION_INVOICE
ITERATIONS = 500
KEYS = (
    "amount",
    "extraction_confidence",
    "has_clause_quote",
    "client_tags",
    "days_since_last_client_invoice",
    "line_items_match_decimal_total",
)
GARBAGE: list[Any] = ["abc", 12.5, [], {}, object(), -1, True, "1.5", None]
BOUNDARY_AMOUNTS = ["0.00", "250.00", "250.01", "249.99", "999.99"]


def _valid(rng: random.Random, key: str) -> Any:
    if key == "amount":
        if rng.random() < 0.3:
            value = rng.choice(BOUNDARY_AMOUNTS)
        else:
            value = f"{rng.randint(0, 999)}.{rng.randint(0, 99):02d}"
        return Decimal(value) if rng.random() < 0.5 else value
    if key == "extraction_confidence":
        return rng.choice([0.0, 0.5, 0.89, 0.9, 0.91, 1.0, Decimal("0.9")])
    if key in ("has_clause_quote", "line_items_match_decimal_total"):
        return rng.random() < 0.5
    if key == "client_tags":
        return rng.choice([[], ["vip"], ["never_auto_contact"]])
    return rng.choice([None, 0, 3, 7, 8, 30])


def _facts(rng: random.Random) -> dict[str, Any]:
    out: dict[str, Any] = {"client": "Acme"}
    for key in KEYS:
        roll = rng.random()
        if roll < 0.15:
            continue
        out[key] = rng.choice(GARBAGE) if roll < 0.3 else _valid(rng, key)
    return out


def _money_ok(v: Any) -> Decimal | None:
    if isinstance(v, Decimal) and v.is_finite() and v >= 0 and v.as_tuple().exponent == -2:
        return v
    if isinstance(v, str) and len(v) >= 4 and v[-3] == "." and v.replace(".", "", 1).isdigit():
        return Decimal(v)
    return None


def _confidence_ok(v: Any) -> Decimal | None:
    if isinstance(v, bool) or not isinstance(v, float | Decimal):
        return None
    d = Decimal(str(v))
    return d if Decimal(0) <= d <= 1 else None


def _days_ok(facts: dict[str, Any]) -> bool:
    if "days_since_last_client_invoice" not in facts:
        return False
    days = facts["days_since_last_client_invoice"]
    return days is None or (isinstance(days, int) and not isinstance(days, bool) and days >= 0)


def _all_valid(f: dict[str, Any]) -> bool:
    return (
        _money_ok(f.get("amount")) is not None
        and _confidence_ok(f.get("extraction_confidence")) is not None
        and isinstance(f.get("has_clause_quote"), bool)
        and isinstance(f.get("client_tags"), list)
        and all(isinstance(t, str) for t in f["client_tags"])
        and _days_ok(f)
        and isinstance(f.get("line_items_match_decimal_total"), bool)
    )


def _oracle_allows(f: dict[str, Any]) -> bool:
    """Independent check: all G-01 conditions hold on valid facts, no ASK/DENY."""
    if not _all_valid(f):
        return False
    amount = _money_ok(f["amount"])
    conf = _confidence_ok(f["extraction_confidence"])
    assert amount is not None and conf is not None
    if amount > Decimal("250.00") or conf < Decimal("0.9"):
        return False
    if f["has_clause_quote"] is not True or f["line_items_match_decimal_total"] is not True:
        return False
    if "never_auto_contact" in f["client_tags"]:
        return False
    days = f["days_since_last_client_invoice"]
    return days is None or days > 7


def _explainer(rng: random.Random) -> Any:
    behaviour = rng.choice(["raise", "timeout", "allow", "long", "valid"])

    async def fn(*_: Any, **__: Any) -> str:
        if behaviour == "raise":
            raise RuntimeError("boom")
        if behaviour == "timeout":
            await asyncio.sleep(10)
        if behaviour == "allow":
            return "ALLOW"
        if behaviour == "long":
            return "A" * 300
        return "Looks fine, send it."

    return fn


async def _run(check: Callable[[dict[str, Any], LeashDecision], None]) -> int:
    rng = random.Random(20261024)
    count = 0
    for _ in range(ITERATIONS):
        facts = _facts(rng)
        service = LeashService(
            StaticPolicyProvider(default_policy()),
            InMemoryDecisionLog(),
            explainer=_explainer(rng),
            explain_timeout_s=0.01,
        )
        expected = evaluate_rules(default_policy(), INVOICE, facts)
        result = await service.decide(INVOICE, facts, "dsc_1")
        assert result.decision == expected[0]
        assert result.rule_ids == expected[1]
        check(facts, result.decision)
        count += 1
    return count


@pytest.mark.asyncio
async def test_ai_never_yields_allow() -> None:
    allows = 0

    def check(facts: dict[str, Any], decision: LeashDecision) -> None:
        nonlocal allows
        if decision == LeashDecision.ALLOW:
            allows += 1
            assert _oracle_allows(facts), facts

    start = time.perf_counter()
    assert await _run(check) == ITERATIONS
    assert time.perf_counter() - start < 30
    assert allows > 0  # the property is not vacuous


@pytest.mark.asyncio
async def test_unknown_facts_never_allow() -> None:
    unknown_seen = 0

    def check(facts: dict[str, Any], decision: LeashDecision) -> None:
        nonlocal unknown_seen
        if not _all_valid(facts):
            unknown_seen += 1
            assert decision != LeashDecision.ALLOW, facts

    await _run(check)
    assert unknown_seen > 0


class _S(str):
    """A str subclass: never acceptable, whatever its value."""


_FLAG_POOL = ["PR-01", "PR-1", "pr-01", "PR-08", " PR-01", "PR-07", "PR-00", "G-01"]
_DECISIONS: list[Any] = [
    "ALLOW", "allow", "ASK", "DENY", " ALLOW", True, 1, None,
    LeashDecision.ALLOW, LeashDecision.ASK, _S("ALLOW"), _S("ASK"),
]  # fmt: skip
_PAYROLL = (GuardAction.PAY_TEAM_MEMBER, GuardAction.SEND_PAYROLL_BATCH)


def _garbage_check(rng: random.Random, depth: int = 0) -> Any:
    roll = rng.random()
    if roll < 0.15:
        return rng.choice([None, True, 0, 1.5, "ALLOW", [], (), object()])
    if roll < 0.3 and depth < 2:
        return {
            "decision": _garbage_check(rng, depth + 1),
            "flags": _garbage_check(rng, depth + 1),
        }
    check: dict[str, Any] = {
        "decision": rng.choice(_DECISIONS),
        "flags": rng.choice([list, tuple])(rng.sample(_FLAG_POOL, rng.randint(0, 2))),
    }
    roll = rng.random()
    if roll < 0.15 and check["flags"]:  # duplicate flags
        check["flags"] = [*check["flags"], check["flags"][0]]
    elif roll < 0.3:  # str-subclass flag
        check["flags"] = [_S("PR-01")]
    if rng.random() < 0.1:
        check["extra"] = 1
    return check


def _is_exact_allow(check: Any) -> bool:
    return (
        type(check) is dict
        and set(check) == {"decision", "flags"}
        and (
            check["decision"] is LeashDecision.ALLOW
            or (type(check["decision"]) is str and check["decision"] == "ALLOW")
        )
        and type(check["flags"]) in (list, tuple)
        and len(check["flags"]) == 0
    )


def test_garbage_payroll_check_never_yields_allow() -> None:
    from backend.app.schemas.contracts import LeashRule

    rng = random.Random(20261105)
    allows = tightened = 0
    for i in range(ITERATIONS):
        action = _PAYROLL[i % 2]
        check = _garbage_check(rng)
        policy = default_policy()
        if rng.random() < 0.25:
            outcome = rng.choice([LeashDecision.ASK, LeashDecision.DENY])
            policy.rules.append(
                LeashRule(
                    id="P-1", applies_to=action, when={}, outcome=outcome,
                    source_text="t",
                )
            )  # fmt: skip
        decision, ids = evaluate_rules(policy, action, {"payroll_check": check})
        extra = any(r.applies_to == action for r in policy.rules)
        if decision == LeashDecision.ALLOW:
            allows += 1
            assert _is_exact_allow(check), check
            assert not extra
            assert ids == ["PAYROLL-CHECKS"]
        elif _is_exact_allow(check):
            tightened += 1
            assert extra
            assert decision in (LeashDecision.ASK, LeashDecision.DENY)
    assert allows > 0
    assert tightened > 0
