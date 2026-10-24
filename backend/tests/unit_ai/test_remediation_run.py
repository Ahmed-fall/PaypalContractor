"""Remediation agent: ``run_remediation`` orchestration (plan Task 6).

PayPal is replaced by ``FakeExecutor`` (the protocol P1 implements); Gemini by
``FakePlanner``. No network, no API keys.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any
from uuid import NAMESPACE_DNS, uuid5

import pytest

from backend.app.ai.auditor_agent import evaluate_payment_compliance
from backend.app.ai.llm import ToolCall
from backend.app.ai.paypal_tools import (
    REMEDIATION_TOOL_NAMES,
    CreateInvoiceParameters,
    GenerateInvoiceQrCodeParameters,
    InvoiceNoteTooLong,
    NothingToInvoice,
    SendInvoiceParameters,
    build_create_invoice_params,
    engine_note_footer,
)
from backend.app.ai.remediation_agent import (
    REMEDIATION_BUDGET_SECONDS,
    REMEDIATION_PROVIDER_TIMEOUT_SECONDS,
    RemediationNotApproved,
    RemediationRun,
    RemediationStepFailed,
    run_remediation,
)
from backend.app.schemas.contracts import DiscrepancyEvaluationResult

from ._auditor_helpers import ACME, ACME_CAPTURE, INITECH, D, milestone

INVOICE_ID = "INV2-TEST"
QR_PNG = "iVBORw0KGgo="


class FakeExecutor:
    """Stands in for P1's PayPalInvoiceExecutor; records every call."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[str] = []
        self.create_params: CreateInvoiceParameters | None = None
        self.request_id: str | None = None
        self.send_params: SendInvoiceParameters | None = None
        self.qr_params: GenerateInvoiceQrCodeParameters | None = None

    def _maybe_fail(self, step: str) -> None:
        self.calls.append(step)
        if self.fail_on == step:
            raise RuntimeError("500 INTERNAL")

    async def create_invoice(
        self, params: CreateInvoiceParameters, request_id: str
    ) -> str:
        self.create_params, self.request_id = params, request_id
        self._maybe_fail("create_invoice")
        return INVOICE_ID

    async def send_invoice(self, params: SendInvoiceParameters) -> None:
        self.send_params = params
        self._maybe_fail("send_invoice")

    async def generate_invoice_qr_code(
        self, params: GenerateInvoiceQrCodeParameters
    ) -> str:
        self.qr_params = params
        self._maybe_fail("generate_invoice_qr_code")
        return QR_PNG


class FakePlanner:
    def __init__(
        self, calls: list[ToolCall] | None = None, *, exc: BaseException | None = None
    ) -> None:
        self.name = "fake-planner"
        self._calls = calls or []
        self._exc = exc
        self.invocations = 0
        self.prompts: list[str] = []

    async def plan(
        self, system_prompt: str, user_prompt: str, timeout: float
    ) -> list[ToolCall]:
        self.invocations += 1
        self.prompts.append(user_prompt)
        if self._exc is not None:
            raise self._exc
        return self._calls


def acme_evaluation() -> DiscrepancyEvaluationResult:
    return asyncio.run(
        evaluate_payment_compliance(ACME, milestone(ACME), **ACME_CAPTURE, writers=[])
    )


def canonical() -> CreateInvoiceParameters:
    return build_create_invoice_params(ACME, milestone(ACME), acme_evaluation())


def exact_plan(args: dict[str, Any] | None = None) -> list[ToolCall]:
    create_args = (
        args if args is not None else canonical().model_dump(exclude_none=True)
    )
    return [
        ToolCall("create_invoice", create_args),
        ToolCall("send_invoice", {"invoice_id": "INV-FROM-MODEL"}),
        ToolCall("generate_invoice_qr_code", {"invoice_id": "INV-FROM-MODEL"}),
    ]


def run(
    executor: FakeExecutor,
    *,
    planners: list[FakePlanner] | None = None,
    confirm_send: bool = True,
    evaluation: DiscrepancyEvaluationResult | None = None,
    contract: Any = ACME,
    code: str = "MS-01",
    discrepancy_id: str = "dsc_01J9Z99",
    note_override: str | None = None,
) -> RemediationRun:
    return asyncio.run(
        run_remediation(
            contract,
            milestone(contract, code),
            evaluation if evaluation is not None else acme_evaluation(),
            executor,
            discrepancy_id=discrepancy_id,
            confirm_send=confirm_send,
            note_override=note_override,
            planners=planners if planners is not None else [],
        )
    )


# --- gates that must stop before PayPal is touched ------------------------------------


def test_requires_confirm_send() -> None:
    executor = FakeExecutor()
    with pytest.raises(RemediationNotApproved):
        run(executor, confirm_send=False)
    assert executor.calls == []


def test_zero_total_never_calls_executor() -> None:
    compliant = asyncio.run(
        evaluate_payment_compliance(
            INITECH,
            milestone(INITECH),
            D("1200.00"),
            "USD",
            date(2026, 10, 10),
            writers=[],
        )
    )
    executor = FakeExecutor()
    with pytest.raises(NothingToInvoice):
        run(executor, evaluation=compliant, contract=INITECH)
    assert executor.calls == []


def test_oversized_override_never_calls_executor() -> None:
    executor = FakeExecutor()
    with pytest.raises(InvoiceNoteTooLong):
        run(executor, note_override="x" * 4000)
    assert executor.calls == []


# --- happy paths ------------------------------------------------------------------------


def test_note_override_reaches_create_invoice() -> None:
    executor = FakeExecutor()
    run(executor, note_override="Please settle by Friday.")
    assert (
        executor.create_params is not None and executor.create_params.note is not None
    )
    assert executor.create_params.note.startswith("Please settle by Friday.")
    assert executor.create_params.note.endswith(engine_note_footer(acme_evaluation()))


def test_valid_gemini_plan_executes_canonical_args() -> None:
    executor = FakeExecutor()
    result = run(executor, planners=[FakePlanner(exact_plan())])
    assert result.planned_by == "gemini"
    assert executor.calls == list(REMEDIATION_TOOL_NAMES)
    assert executor.create_params == canonical()
    assert result.invoice_id == INVOICE_ID
    assert result.qr_code_base64 == QR_PNG and result.qr_error is None
    assert result.tool_calls == REMEDIATION_TOOL_NAMES


def test_model_send_args_never_reach_executor() -> None:
    """The model proposes invoice_id INV-FROM-MODEL; only the created id is used."""
    executor = FakeExecutor()
    run(executor, planners=[FakePlanner(exact_plan())])
    assert executor.send_params is not None and executor.qr_params is not None
    assert executor.send_params.invoice_id == INVOICE_ID
    assert executor.qr_params.invoice_id == INVOICE_ID


@pytest.mark.parametrize("tamper", ["currency-eur", "amount-zero"])
def test_tampered_plan_still_executes_canonical_args(tamper: str) -> None:
    args = canonical().model_dump(exclude_none=True)
    if tamper == "currency-eur":
        args["currency_code"] = "EUR"
    else:
        args["items"][0]["unit_amount"]["value"] = "0.00"
    executor = FakeExecutor()
    result = run(executor, planners=[FakePlanner(exact_plan(args))])
    assert result.planned_by == "deterministic"
    assert executor.create_params == canonical()


def test_rejected_plan_tries_next_planner() -> None:
    args = canonical().model_dump(exclude_none=True)
    args["currency_code"] = "EUR"
    tampered, exact = FakePlanner(exact_plan(args)), FakePlanner(exact_plan())
    result = run(FakeExecutor(), planners=[tampered, exact])
    assert result.planned_by == "gemini"
    assert (tampered.invocations, exact.invocations) == (1, 1)


def test_planner_failure_uses_deterministic_sequence() -> None:
    executor = FakeExecutor()
    result = run(executor, planners=[FakePlanner(exc=TimeoutError())])
    assert result.planned_by == "deterministic"
    assert executor.calls == list(REMEDIATION_TOOL_NAMES)


def test_request_id_is_deterministic_per_discrepancy() -> None:
    first, second = FakeExecutor(), FakeExecutor()
    a, b = run(first), run(second)
    expected = str(uuid5(NAMESPACE_DNS, "dsc_01J9Z99:remediate"))
    assert a.request_id == b.request_id == expected
    assert first.request_id == second.request_id == expected
    assert run(FakeExecutor(), discrepancy_id="dsc_OTHER").request_id != expected


# --- failures after PayPal was touched --------------------------------------------------


def test_create_failure_raises_without_invoice_id() -> None:
    executor = FakeExecutor(fail_on="create_invoice")
    with pytest.raises(RemediationStepFailed) as exc:
        run(executor)
    assert exc.value.step == "create_invoice"
    assert exc.value.invoice_id is None
    assert exc.value.__cause__ is not None
    assert executor.calls == ["create_invoice"]


def test_send_failure_reports_created_invoice_id() -> None:
    executor = FakeExecutor(fail_on="send_invoice")
    with pytest.raises(RemediationStepFailed) as exc:
        run(executor)
    assert exc.value.step == "send_invoice"
    assert exc.value.invoice_id == INVOICE_ID
    assert executor.calls == ["create_invoice", "send_invoice"]


def test_qr_failure_is_not_fatal() -> None:
    """AGENTS.md Fallback B: the invoice is sent; P1 builds a local QR instead."""
    executor = FakeExecutor(fail_on="generate_invoice_qr_code")
    result = run(executor)
    assert result.invoice_id == INVOICE_ID
    assert result.qr_code_base64 is None
    assert result.qr_error is not None and "500 INTERNAL" in result.qr_error


def test_send_invoice_carries_no_note() -> None:
    executor = FakeExecutor()
    run(executor)
    assert executor.send_params is not None
    assert "note" not in executor.send_params.model_dump(exclude_none=True)
    assert executor.send_params.send_to_recipient is True


def test_no_api_key_needs_no_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    evaluation = acme_evaluation()
    executor = FakeExecutor()
    start = time.perf_counter()
    result = asyncio.run(
        run_remediation(
            ACME,
            milestone(ACME),
            evaluation,
            executor,
            discrepancy_id="dsc_01J9Z99",
            confirm_send=True,
        )
    )
    assert time.perf_counter() - start < 0.2
    assert result.planned_by == "deterministic"
    assert executor.calls == list(REMEDIATION_TOOL_NAMES)


# --- planning budget and prompt size (live measurement: ~5 s per Gemini plan) -----------


def test_remediation_planning_budget_is_7_seconds() -> None:
    """Owner decision 2026-10-24: Gemini needs ~5 s; only the Approve click waits."""
    assert REMEDIATION_BUDGET_SECONDS == 7.0
    assert REMEDIATION_PROVIDER_TIMEOUT_SECONDS < REMEDIATION_BUDGET_SECONDS


def test_planner_prompt_omits_unchecked_long_fields() -> None:
    """The model only re-types fields the validator compares (shorter = faster)."""
    planner = FakePlanner(exact_plan())
    run(FakeExecutor(), planners=[planner])
    prompt = planner.prompts[0]
    evaluation = acme_evaluation()
    assert "This supplemental invoice relates to" not in prompt
    for item in evaluation.remediation_line_items:
        assert item.description not in prompt
        assert item.name in prompt
    assert "150.00" in prompt and "sb-buyer@personal.example.com" in prompt


def test_compact_model_answer_is_still_accepted() -> None:
    """A model that copies the compact arguments (no note/descriptions) agrees."""
    compact = canonical().model_dump(
        exclude_none=True,
        exclude={
            "note": True,
            "reference": True,
            "items": {"__all__": {"description"}},
        },
    )
    result = run(FakeExecutor(), planners=[FakePlanner(exact_plan(compact))])
    assert result.planned_by == "gemini"


# --- retry / resume support for P1 (code review 2) ----------------------------------------


class BadIdExecutor(FakeExecutor):
    def __init__(self, returned: Any) -> None:
        super().__init__()
        self.returned = returned

    async def create_invoice(
        self, params: CreateInvoiceParameters, request_id: str
    ) -> str:
        self.calls.append("create_invoice")
        return self.returned  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "returned", ["", "   ", None, 123], ids=["empty", "blank", "none", "int"]
)
def test_bad_invoice_id_from_executor_is_a_create_failure(returned: Any) -> None:
    executor = BadIdExecutor(returned)
    with pytest.raises(RemediationStepFailed) as exc:
        run(executor)
    assert exc.value.step == "create_invoice"
    assert exc.value.invoice_id is None
    assert executor.calls == ["create_invoice"]


def test_existing_invoice_id_resumes_at_send_without_planning_or_create() -> None:
    """After RemediationStepFailed(step="send_invoice", invoice_id=X), P1 resumes with X."""
    planner = FakePlanner(exact_plan())
    executor = FakeExecutor()
    result = asyncio.run(
        run_remediation(
            ACME,
            milestone(ACME),
            acme_evaluation(),
            executor,
            discrepancy_id="dsc_01J9Z99",
            confirm_send=True,
            existing_invoice_id="INV2-EXISTING",
            planners=[planner],
        )
    )
    assert executor.calls == ["send_invoice", "generate_invoice_qr_code"]
    assert executor.send_params is not None
    assert executor.send_params.invoice_id == "INV2-EXISTING"
    assert planner.invocations == 0
    assert result.invoice_id == "INV2-EXISTING"
    assert result.planned_by == "deterministic"
    assert result.tool_calls == ("send_invoice", "generate_invoice_qr_code")


def test_resume_still_requires_approval() -> None:
    executor = FakeExecutor()
    with pytest.raises(RemediationNotApproved):
        asyncio.run(
            run_remediation(
                ACME,
                milestone(ACME),
                acme_evaluation(),
                executor,
                discrepancy_id="dsc_01J9Z99",
                confirm_send=False,
                existing_invoice_id="INV2-EXISTING",
            )
        )
    assert executor.calls == []


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_existing_invoice_id_is_rejected(bad: str) -> None:
    executor = FakeExecutor()
    with pytest.raises(ValueError):
        asyncio.run(
            run_remediation(
                ACME,
                milestone(ACME),
                acme_evaluation(),
                executor,
                discrepancy_id="dsc_01J9Z99",
                confirm_send=True,
                existing_invoice_id=bad,
            )
        )
    assert executor.calls == []


@pytest.mark.parametrize(
    "value", ["false", "yes", 1, None], ids=["str-false", "str-yes", "int", "none"]
)
def test_only_literal_true_approves(value: Any) -> None:
    """AGENTS.md section 8: only an explicit confirm_send=True may create an invoice."""
    executor = FakeExecutor()
    with pytest.raises(RemediationNotApproved):
        run(executor, confirm_send=value)
    assert executor.calls == []
