"""Guarded remediation agent: Gemini starts the PayPal invoice workflow by tool calls.

``run_remediation`` creates, sends and QR-codes the remediation invoice through a
``PayPalInvoiceExecutor`` that the backend (P1) implements; this package never calls
PayPal. Gemini may only *start* the fixed sequence ``create_invoice → send_invoice →
generate_invoice_qr_code``; ``validate_tool_plan`` checks its proposal against the
engine's canonical arguments, and only those canonical arguments are ever executed.
Anything else (another tool, a repeated or reordered step, or a different amount,
currency, recipient, item list or extra field) is rejected.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_DNS, uuid5

from pydantic import ValidationError

from ..schemas.contracts import DiscrepancyEvaluationResult, ExtractedMilestoneSpec
from .auditor_agent import ContractTerms
from .llm import (
    GeminiModel,
    ToolCall,
    configured_key,
    gemini_models_from_env,
    run_with_failover,
)
from .paypal_tools import (
    REMEDIATION_TOOL_NAMES,
    CreateInvoiceParameters,
    GenerateInvoiceQrCodeParameters,
    SendInvoiceParameters,
    build_create_invoice_params,
    tool_declarations,
)
from .prompts import REMEDIATION_SYSTEM_PROMPT, build_remediation_prompt

# Live measurement 2026-10-24: Gemini needs ~5 s to plan the tool calls. Remediation runs
# on the human Approve click (not on /capture), so the owner chose a 7 s budget here.
REMEDIATION_BUDGET_SECONDS = 7.0
REMEDIATION_PROVIDER_TIMEOUT_SECONDS = 6.5
# Fields the validator never compares: hiding them shortens what the model re-types.
_UNCOMPARED_FIELDS: dict[str, Any] = {
    "note": True,
    "reference": True,
    "items": {"__all__": {"description"}},
}


def _normalise_numbers(node: Any) -> Any:
    """Models often send numbers where the toolkit expects strings (``150`` vs ``"150"``)."""
    if isinstance(node, bool):
        return node
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, dict):
        return {key: _normalise_numbers(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_normalise_numbers(item) for item in node]
    return node


def _as_seen(text: str) -> str:
    """The prompt shows contract-derived "<" as "&lt;" (tag escaping); a faithful copy
    of what the model saw must still compare equal."""
    return text.replace("&lt;", "<")


def _binding_view(
    params: CreateInvoiceParameters,
) -> tuple[
    str,
    list[tuple[str, str]],
    list[tuple[str, Decimal, str, Decimal]],
]:
    """Everything that decides who is billed how much, with amounts as ``Decimal``."""
    recipients = [
        (
            (
                _as_seen(r.billing_info.business_name or ""),
                _as_seen(r.billing_info.email_address or ""),
            )
            if r.billing_info is not None
            else ("", "")
        )
        for r in params.primary_recipients
    ]
    items = [
        (
            _as_seen(item.name),
            Decimal(item.unit_amount.value),
            item.unit_amount.currency_code,
            Decimal(item.quantity),
        )
        for item in params.items
    ]
    return params.currency_code, recipients, items


def validate_tool_plan(
    calls: Sequence[ToolCall], canonical: CreateInvoiceParameters
) -> bool:
    """True only for a non-empty, in-order prefix of the remediation sequence that starts
    with ``create_invoice`` whose currency, full recipient list and items equal the
    canonical ones, with no fields the engine did not set. Never raises.

    A prefix is enough because Gemini plans one step per turn (send and QR need the id
    that create returns); the engine completes the remaining steps itself.
    """
    names = [call.name for call in calls]
    if not names or names != list(REMEDIATION_TOOL_NAMES[: len(names)]):
        return False
    try:
        proposed = CreateInvoiceParameters.model_validate(
            _normalise_numbers(calls[0].args)
        )
        return _binding_view(proposed) == _binding_view(canonical)
    except (ValidationError, InvalidOperation, TypeError, ValueError):
        return False


# --- orchestration (plan Task 6) --------------------------------------------------------


class PayPalInvoiceExecutor(Protocol):
    """Implemented by P1 with the backend's PayPal client; P2 never calls PayPal.

    ``create_invoice`` must send ``request_id`` as the ``PayPal-Request-Id`` header
    (PIT-06) and should keep the invoice number, status and payer-view link from
    PayPal's response for ``RemediationInvoiceRecord``.
    """

    async def create_invoice(
        self, params: CreateInvoiceParameters, request_id: str
    ) -> str: ...  # returns the PayPal invoice id

    async def send_invoice(self, params: SendInvoiceParameters) -> None: ...

    async def generate_invoice_qr_code(
        self, params: GenerateInvoiceQrCodeParameters
    ) -> str: ...  # returns a base64 PNG


class RemediationPlanner(Protocol):
    name: str

    async def plan(
        self, system_prompt: str, user_prompt: str, timeout: float
    ) -> list[ToolCall]: ...


class GeminiRemediationPlanner(GeminiModel):
    async def plan(
        self, system_prompt: str, user_prompt: str, timeout: float
    ) -> list[ToolCall]:
        return await self.generate_tool_calls(
            system_prompt,
            user_prompt,
            tool_declarations()[: len(REMEDIATION_TOOL_NAMES)],
            REMEDIATION_TOOL_NAMES,
        )


def default_planners() -> list[RemediationPlanner]:
    key = configured_key(os.environ.get("GEMINI_API_KEY"))
    if not key:
        return []
    return [GeminiRemediationPlanner(key, model) for model in gemini_models_from_env()]


class RemediationNotApproved(PermissionError):
    """``confirm_send`` was not given: a human must approve before any invoice exists."""


class RemediationStepFailed(RuntimeError):
    """A PayPal step failed. ``invoice_id`` is set when the draft was already created,
    so the caller can retry the send without creating a duplicate invoice."""

    def __init__(
        self, step: Literal["create_invoice", "send_invoice"], invoice_id: str | None
    ) -> None:
        super().__init__(f"remediation step {step} failed (invoice_id={invoice_id})")
        self.step = step
        self.invoice_id = invoice_id


class _PlanRejected(ValueError):
    """The model's proposal did not match the engine's canonical plan."""


@dataclass(frozen=True)
class RemediationRun:
    invoice_id: str
    qr_code_base64: str | None
    qr_error: str | None
    request_id: str
    planned_by: Literal["gemini", "deterministic"]
    tool_calls: tuple[str, ...]


def remediation_request_id(discrepancy_id: str) -> str:
    """Deterministic PayPal-Request-Id: retries of one discrepancy never duplicate."""
    return str(uuid5(NAMESPACE_DNS, f"{discrepancy_id}:remediate"))


async def run_remediation(
    contract: ContractTerms,
    milestone: ExtractedMilestoneSpec,
    evaluation: DiscrepancyEvaluationResult,
    executor: PayPalInvoiceExecutor,
    *,
    discrepancy_id: str,
    confirm_send: bool,
    note_override: str | None = None,
    existing_invoice_id: str | None = None,
    planners: Sequence[RemediationPlanner] | None = None,
    budget_seconds: float = REMEDIATION_BUDGET_SECONDS,
) -> RemediationRun:
    """Create, send and QR-code the remediation invoice for an approved discrepancy.

    Gemini is asked to start the workflow by tool calls; whatever it proposes, only the
    engine's canonical arguments are executed. A QR failure is not fatal (Fallback B).

    Retry: after ``RemediationStepFailed(step="create_invoice")`` call again unchanged
    (the same ``request_id`` makes PayPal return the existing draft). After
    ``RemediationStepFailed(step="send_invoice", invoice_id=X)`` call again with
    ``existing_invoice_id=X``: planning and create are skipped, send and QR run.
    """
    if confirm_send is not True:  # only an explicit True approves (AGENTS.md section 8)
        raise RemediationNotApproved("remediation requires confirm_send=True")
    if existing_invoice_id is not None and not existing_invoice_id.strip():
        raise ValueError("existing_invoice_id must be a non-blank PayPal invoice id")

    create_params = build_create_invoice_params(
        contract, milestone, evaluation, note_override=note_override
    )
    request_id = remediation_request_id(discrepancy_id)

    if existing_invoice_id is not None:
        invoice_id = existing_invoice_id.strip()
        agreed: list[ToolCall] | None = None
        steps: tuple[str, ...] = REMEDIATION_TOOL_NAMES[1:]
    else:
        agreed = await _plan(create_params, evaluation, planners, budget_seconds)
        invoice_id = await _create(executor, create_params, request_id)
        steps = REMEDIATION_TOOL_NAMES

    try:
        await executor.send_invoice(SendInvoiceParameters(invoice_id=invoice_id))
    except Exception as exc:
        raise RemediationStepFailed("send_invoice", invoice_id) from exc

    qr_code: str | None = None
    qr_error: str | None = None
    try:
        qr_code = await executor.generate_invoice_qr_code(
            GenerateInvoiceQrCodeParameters(invoice_id=invoice_id)
        )
    except Exception as exc:  # noqa: BLE001 - Fallback B: the invoice is already sent
        qr_error = f"{type(exc).__name__}: {exc}"[:200]

    return RemediationRun(
        invoice_id=invoice_id,
        qr_code_base64=qr_code,
        qr_error=qr_error,
        request_id=request_id,
        planned_by="gemini" if agreed is not None else "deterministic",
        tool_calls=steps,
    )


async def _plan(
    create_params: CreateInvoiceParameters,
    evaluation: DiscrepancyEvaluationResult,
    planners: Sequence[RemediationPlanner] | None,
    budget_seconds: float,
) -> list[ToolCall] | None:
    """Ask Gemini to start the workflow; ``None`` when no planner agreed in time."""
    chain = default_planners() if planners is None else list(planners)
    prompt = build_remediation_prompt(
        create_params.model_dump_json(exclude_none=True, exclude=_UNCOMPARED_FIELDS),
        evaluation.violated_clause_quote,
    )

    async def attempt(planner: RemediationPlanner, timeout: float) -> list[ToolCall]:
        calls = await planner.plan(REMEDIATION_SYSTEM_PROMPT, prompt, timeout)
        if not validate_tool_plan(calls, create_params):
            raise _PlanRejected(f"{planner.name} proposed {[c.name for c in calls]}")
        return calls

    return await run_with_failover(
        chain,
        attempt,
        purpose="remediation plan",
        budget_seconds=budget_seconds,
        provider_timeout=REMEDIATION_PROVIDER_TIMEOUT_SECONDS,
    )


async def _create(
    executor: PayPalInvoiceExecutor,
    create_params: CreateInvoiceParameters,
    request_id: str,
) -> str:
    try:
        invoice_id: object = await executor.create_invoice(create_params, request_id)
    except Exception as exc:
        raise RemediationStepFailed("create_invoice", None) from exc
    # The executor is outside this package: never pass on an unusable id.
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        raise RemediationStepFailed("create_invoice", None)
    return invoice_id.strip()
