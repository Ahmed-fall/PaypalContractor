"""``decide()``: rules, then an optional AI explanation, then one log row.

The decision always comes from ``evaluate_rules``. The explainer only supplies the
``reason`` text; any failure, timeout or unusable output falls back to the template.
Only payroll Ask/Deny asks the explainer for the LLM (``use_llm=True``); the invoice
guard (hook H1) and payroll Allow get its instant template.
Exactly one record is appended per call, and the log has no way to change or remove
a row once written.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from ulid import ULID

from ..schemas.contracts import GuardAction, GuardResult, LeashDecision, LeashPolicy
from .explain import MAX_REASON_LEN, template_reason
from .guard import evaluate_rules, parse_money
from .policy import default_policy


class Explainer(Protocol):
    """P2's ``explain_decision`` (Interface 3)."""

    async def __call__(
        self,
        action: GuardAction,
        decision: LeashDecision,
        rule_ids: list[str],
        facts: dict[str, Any],
        *,
        use_llm: bool,
    ) -> str: ...


SubjectType = Literal["discrepancy", "payroll_line", "payroll_run"]

SUBJECT_TYPES: dict[GuardAction, SubjectType] = {
    GuardAction.SEND_REMEDIATION_INVOICE: "discrepancy",
    GuardAction.PAY_TEAM_MEMBER: "payroll_line",
    GuardAction.SEND_PAYROLL_BATCH: "payroll_run",
}
# Only these actions' Ask/Deny decisions are worth an LLM explanation.
PAYROLL_ACTIONS: frozenset[GuardAction] = frozenset(
    {GuardAction.PAY_TEAM_MEMBER, GuardAction.SEND_PAYROLL_BATCH}
)
EXPLAIN_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    policy_version: int
    action_type: GuardAction
    subject_type: SubjectType
    subject_id: str
    decision: LeashDecision
    rule_ids: tuple[str, ...]
    facts: dict[str, Any]
    amount: Decimal | None
    reason: str
    actor: Literal["leash", "owner"]
    created_at: datetime


class DecisionLog(Protocol):
    """Append-only: there is deliberately no other mutator."""

    async def append(self, record: DecisionRecord) -> None: ...


class PolicyProvider(Protocol):
    async def active_policy(self) -> LeashPolicy: ...


class InMemoryDecisionLog:
    def __init__(self) -> None:
        self._records: list[DecisionRecord] = []

    async def append(self, record: DecisionRecord) -> None:
        self._records.append(record)

    @property
    def records(self) -> tuple[DecisionRecord, ...]:
        return tuple(self._records)


class StaticPolicyProvider:
    def __init__(self, policy: LeashPolicy) -> None:
        self._policy = policy

    async def active_policy(self) -> LeashPolicy:
        return self._policy


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _json_safe(value: Any) -> Any:
    """A fresh JSON-safe copy: Decimal becomes str, anything odd becomes repr()."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return repr(value)


class LeashService:
    def __init__(
        self,
        policies: PolicyProvider,
        log: DecisionLog,
        explainer: Explainer | None = None,
        explain_timeout_s: float = EXPLAIN_TIMEOUT_S,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._policies = policies
        self._log = log
        self._explainer = explainer
        self._timeout = explain_timeout_s
        self._clock = clock

    async def _explain(
        self,
        action: GuardAction,
        decision: LeashDecision,
        rule_ids: list[str],
        snapshot: dict[str, Any],
    ) -> str:
        fallback = template_reason(action, decision, rule_ids, snapshot)
        if self._explainer is None:
            return fallback
        use_llm = action in PAYROLL_ACTIONS and decision != LeashDecision.ALLOW
        try:
            text = await asyncio.wait_for(
                self._explainer(
                    action,
                    decision,
                    list(rule_ids),
                    _json_safe(snapshot),
                    use_llm=use_llm,
                ),
                self._timeout,
            )
        except Exception:  # noqa: BLE001 - any explainer failure uses the template
            return fallback
        if isinstance(text, str) and 0 < len(text.strip()) <= MAX_REASON_LEN:
            return text.strip()
        return fallback

    async def decide(
        self, action: GuardAction, facts: dict[str, Any], subject_id: str
    ) -> GuardResult:
        # Snapshot first: nothing awaited below may change what gets recorded.
        snapshot = _json_safe(facts)
        amount = parse_money(facts.get("amount"))
        policy = await self._policies.active_policy()
        decision, rule_ids = evaluate_rules(policy, action, facts)
        reason = await self._explain(action, decision, rule_ids, snapshot)
        decision_id = "lsd_" + str(ULID()).lower()
        record = DecisionRecord(
            decision_id=decision_id,
            policy_version=policy.version,
            action_type=action,
            subject_type=SUBJECT_TYPES[action],
            subject_id=subject_id,
            decision=decision,
            rule_ids=tuple(rule_ids),
            facts=snapshot,
            amount=amount,
            reason=reason,
            actor="leash",
            created_at=self._clock(),
        )
        await self._log.append(record)
        return GuardResult(
            decision=decision,
            rule_ids=list(rule_ids),
            reason=reason,
            decision_id=decision_id,
        )


def _load_explainer() -> Explainer | None:
    """P2's ``explain_decision`` if ``..ai.leash`` exists, else None."""
    try:
        base = __name__.rsplit(".", 2)[0]  # backend.app
        name = f"{base}.ai.leash"
        if importlib.util.find_spec(name) is None:
            return None
        module = importlib.import_module(name)
        fn = getattr(module, "explain_decision", None)
        return fn if callable(fn) else None
    except Exception:  # noqa: BLE001 - a broken optional module means no explainer
        return None


_service: LeashService | None = None


def configure(service: LeashService) -> None:
    global _service
    _service = service


def _default_service() -> LeashService:
    return LeashService(
        StaticPolicyProvider(default_policy()),
        InMemoryDecisionLog(),
        explainer=_load_explainer(),
    )


async def decide(
    action: GuardAction, facts: dict[str, Any], subject_id: str
) -> GuardResult:
    global _service
    if _service is None:
        _service = _default_service()
    return await _service.decide(action, facts, subject_id)
