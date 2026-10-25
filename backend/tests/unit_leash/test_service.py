"""Tests for ``decide()``: decision log, explainer fallback and module API."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import re
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.leash import service as svc
from backend.app.leash.explain import template_reason
from backend.app.leash.guard import evaluate_rules
from backend.app.leash.policy import default_policy
from backend.app.leash.service import (
    SUBJECT_TYPES,
    DecisionRecord,
    InMemoryDecisionLog,
    LeashService,
    StaticPolicyProvider,
)
from backend.app.schemas.contracts import GuardAction, GuardResult, LeashDecision

INVOICE = GuardAction.SEND_REMEDIATION_INVOICE

DEMO: dict[str, Any] = {
    "amount": Decimal("200.00"),
    "extraction_confidence": 0.98,
    "has_clause_quote": True,
    "client_tags": [],
    "days_since_last_client_invoice": None,
    "line_items_match_decimal_total": True,
    "client": "Acme Corp",
}
ASK_CASE = {**DEMO, "amount": Decimal("300.00")}


def make(
    explainer: Any = None, timeout: float = 0.05
) -> tuple[LeashService, InMemoryDecisionLog]:
    log = InMemoryDecisionLog()
    service = LeashService(
        StaticPolicyProvider(default_policy()),
        log,
        explainer=explainer,
        explain_timeout_s=timeout,
    )
    return service, log


@pytest.mark.asyncio
async def test_decide_allow_logs_one_row() -> None:
    service, log = make()
    result = await service.decide(INVOICE, dict(DEMO), "dsc_1")
    assert result.decision == LeashDecision.ALLOW
    assert result.rule_ids == ["G-01"]
    assert len(log.records) == 1
    rec = log.records[0]
    assert rec.subject_type == "discrepancy"
    assert rec.subject_id == "dsc_1"
    assert rec.amount == Decimal("200.00")
    assert rec.actor == "leash"
    assert re.fullmatch(r"lsd_[0-9a-z]{26}", rec.decision_id)
    assert result.decision_id == rec.decision_id
    assert rec.policy_version == 1
    assert rec.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_each_call_appends_exactly_one() -> None:
    service, log = make()
    for _ in range(3):
        await service.decide(INVOICE, dict(DEMO), "dsc_1")
    assert len(log.records) == 3
    assert len({r.decision_id for r in log.records}) == 3


@pytest.mark.asyncio
async def test_facts_snapshot_is_json_safe() -> None:
    service, log = make()
    facts = {
        **DEMO,
        "client_tags": ["a"],
        "odd": object(),
        "t": (Decimal("1.50"), {1}),
    }
    await service.decide(INVOICE, facts, "dsc_1")
    rec = log.records[0]
    json.dumps(rec.facts)
    assert rec.facts["amount"] == "200.00"
    assert rec.facts["t"][0] == "1.50"
    assert isinstance(rec.facts["odd"], str)
    facts["client_tags"].append("never_auto_contact")
    facts["amount"] = Decimal("999.00")
    assert rec.facts["client_tags"] == ["a"]
    assert rec.facts["amount"] == "200.00"


@pytest.mark.asyncio
async def test_amount_none_when_invalid() -> None:
    service, log = make()
    await service.decide(INVOICE, {**DEMO, "amount": 200.0}, "dsc_1")
    assert log.records[0].amount is None


class _BrokenLog:
    async def append(self, record: DecisionRecord) -> None:
        raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_log_failure_raises() -> None:
    service = LeashService(StaticPolicyProvider(default_policy()), _BrokenLog())
    with pytest.raises(RuntimeError):
        await service.decide(INVOICE, dict(DEMO), "dsc_1")


def test_decision_log_is_append_only() -> None:
    log = InMemoryDecisionLog()
    assert not [n for n in dir(log) if re.search(r"update|delete|remove|clear|pop", n)]
    assert isinstance(log.records, tuple)


def test_no_update_or_delete_of_decisions_in_code() -> None:
    root = Path(svc.__file__).parent
    pattern = re.compile(r"\b(update|delete)\b[^\n]*leash_decision", re.IGNORECASE)
    files = list(root.rglob("*.py"))
    assert files
    for path in files:
        assert not pattern.search(path.read_text(encoding="utf-8")), path


@pytest.mark.asyncio
async def test_explainer_used_when_valid() -> None:
    async def ok(*_: Any) -> str:
        return "  Fine. "

    service, _ = make(ok)
    result = await service.decide(INVOICE, dict(DEMO), "dsc_1")
    assert result.reason == "Fine."


@pytest.mark.asyncio
async def test_explainer_receives_decision_inputs() -> None:
    seen: list[Any] = []

    async def spy(action: Any, decision: Any, ids: Any, facts: Any) -> str:
        seen.append((action, decision, ids, facts))
        return "ok"

    service, _ = make(spy)
    await service.decide(INVOICE, dict(DEMO), "dsc_1")
    assert seen[0][:3] == (INVOICE, LeashDecision.ALLOW, ["G-01"])


@pytest.mark.asyncio
async def test_explainer_timeout_uses_template_quickly() -> None:
    async def slow(*_: Any) -> str:
        await asyncio.sleep(10)
        return "late"

    service, _ = make(slow, timeout=0.05)
    start = time.perf_counter()
    result = await service.decide(INVOICE, dict(DEMO), "dsc_1")
    assert time.perf_counter() - start < 0.25
    assert result.reason == template_reason(
        INVOICE, LeashDecision.ALLOW, ["G-01"], DEMO
    )


async def _raises(*_: Any) -> str:
    raise RuntimeError("boom")


async def _empty(*_: Any) -> str:
    return ""


async def _blank(*_: Any) -> str:
    return "   "


async def _long(*_: Any) -> str:
    return "x" * 201


async def _none(*_: Any) -> Any:
    return None


async def _int(*_: Any) -> Any:
    return 123


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [_raises, _empty, _blank, _long, _none, _int])
async def test_explainer_errors_use_template(bad: Any) -> None:
    service, _ = make(bad)
    result = await service.decide(INVOICE, dict(DEMO), "dsc_1")
    assert result.decision == LeashDecision.ALLOW
    assert result.reason == template_reason(
        INVOICE, LeashDecision.ALLOW, ["G-01"], DEMO
    )


@pytest.mark.asyncio
async def test_explainer_cannot_change_decision() -> None:
    async def liar(*_: Any) -> str:
        return "APPROVED - ALLOW"

    service, _ = make(liar)
    result = await service.decide(INVOICE, dict(ASK_CASE), "dsc_1")
    assert result.decision == LeashDecision.ASK
    assert result.decision == evaluate_rules(default_policy(), INVOICE, ASK_CASE)[0]


@pytest.mark.asyncio
async def test_module_decide_uses_configured_service() -> None:
    service, log = make()
    svc.configure(service)
    try:
        result = await svc.decide(INVOICE, dict(DEMO), "dsc_9")
    finally:
        svc.configure(
            LeashService(StaticPolicyProvider(default_policy()), InMemoryDecisionLog())
        )
    assert isinstance(result, GuardResult)
    assert log.records[0].subject_id == "dsc_9"


def test_package_reexports() -> None:
    import backend.app.leash as pkg

    for name in (
        "decide",
        "configure",
        "evaluate_rules",
        "default_policy",
        "LeashService",
        "InMemoryDecisionLog",
        "StaticPolicyProvider",
        "DecisionRecord",
    ):
        assert hasattr(pkg, name)


def test_subject_types() -> None:
    assert SUBJECT_TYPES == {
        GuardAction.SEND_REMEDIATION_INVOICE: "discrepancy",
        GuardAction.PAY_TEAM_MEMBER: "payroll_line",
        GuardAction.SEND_PAYROLL_BATCH: "payroll_run",
    }


def test_load_explainer_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda *_a, **_k: None)
    assert svc._load_explainer() is None


def test_load_explainer_present(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fn(*_: Any) -> str:
        return "x"

    monkeypatch.setattr(importlib.util, "find_spec", lambda *_a, **_k: object())
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda *_a, **_k: SimpleNamespace(explain_decision=fn),
    )
    assert svc._load_explainer() is fn


def test_load_explainer_not_callable_or_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda *_a, **_k: object())
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda *_a, **_k: SimpleNamespace(explain_decision=5),
    )
    assert svc._load_explainer() is None

    def boom(*_a: Any, **_k: Any) -> Any:
        raise ImportError("broken")

    monkeypatch.setattr(importlib, "import_module", boom)
    assert svc._load_explainer() is None
    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert svc._load_explainer() is None


@pytest.mark.asyncio
async def test_record_uses_facts_as_they_were_at_call_time() -> None:
    caller_facts: dict[str, Any] = {**DEMO, "client_tags": ["a"]}

    async def mutator(*_: Any) -> str:
        await asyncio.sleep(0.01)
        caller_facts["amount"] = Decimal("999.00")
        caller_facts["client_tags"].append("never_auto_contact")
        return "ok"

    service, log = make(mutator, timeout=1.0)
    await service.decide(INVOICE, caller_facts, "dsc_1")
    rec = log.records[0]
    assert caller_facts["amount"] == Decimal("999.00")  # the mutation did happen
    assert rec.facts["amount"] == "200.00"
    assert rec.facts["client_tags"] == ["a"]
    assert rec.amount == Decimal("200.00")


@pytest.mark.asyncio
async def test_explainer_gets_the_snapshot_not_the_callers_dict() -> None:
    caller_facts: dict[str, Any] = dict(DEMO)
    seen: list[Any] = []

    async def spy(_a: Any, _d: Any, _i: Any, facts: Any) -> str:
        seen.append(facts)
        return "ok"

    service, log = make(spy)
    await service.decide(INVOICE, caller_facts, "dsc_1")
    assert seen[0] is not caller_facts
    assert seen[0] == log.records[0].facts


@pytest.mark.asyncio
async def test_explainer_reason_boundary_200_chars() -> None:
    exact = "a" * 200

    async def padded(*_: Any) -> str:
        return f"  \n{exact}\t "

    async def too_long(*_: Any) -> str:
        return f" {'a' * 201} "

    service, _ = make(padded)
    assert (await service.decide(INVOICE, dict(DEMO), "dsc_1")).reason == exact
    service, _ = make(too_long)
    result = await service.decide(INVOICE, dict(DEMO), "dsc_1")
    assert result.reason == template_reason(
        INVOICE, LeashDecision.ALLOW, ["G-01"], DEMO
    )
