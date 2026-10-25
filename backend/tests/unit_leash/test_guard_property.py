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
    if (
        isinstance(v, Decimal)
        and v.is_finite()
        and v >= 0
        and v.as_tuple().exponent == -2
    ):
        return v
    if (
        isinstance(v, str)
        and len(v) >= 4
        and v[-3] == "."
        and v.replace(".", "", 1).isdigit()
    ):
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
    return days is None or (
        isinstance(days, int) and not isinstance(days, bool) and days >= 0
    )


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
    if (
        f["has_clause_quote"] is not True
        or f["line_items_match_decimal_total"] is not True
    ):
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
