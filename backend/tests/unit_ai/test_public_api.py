"""The surface P1 imports: ``from app.ai import extract_contract_rules, evaluate_payment_compliance``."""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backend.app import ai
from backend.app.schemas.contracts import (
    ContractExtractionResult,
    DiscrepancyEvaluationResult,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "contracts"


def test_exports_exactly_the_public_surface() -> None:
    assert sorted(ai.__all__) == [
        "ContractParseError",
        "InvoiceInvariantError",
        "InvoiceNoteTooLong",
        "NothingToInvoice",
        "PayPalInvoiceExecutor",
        "RemediationNotApproved",
        "RemediationRun",
        "RemediationStepFailed",
        "build_create_invoice_params",
        "evaluate_payment_compliance",
        "extract_contract_rules",
        "remediation_request_id",
        "run_remediation",
    ]
    for name in ai.__all__:
        assert hasattr(ai, name), name
    assert inspect.iscoroutinefunction(ai.extract_contract_rules)
    assert inspect.iscoroutinefunction(ai.evaluate_payment_compliance)
    assert inspect.iscoroutinefunction(ai.run_remediation)


def test_run_remediation_signature() -> None:
    params = inspect.signature(ai.run_remediation).parameters
    assert list(params)[:4] == ["contract", "milestone", "evaluation", "executor"]
    for name in (
        "discrepancy_id",
        "confirm_send",
        "note_override",
        "existing_invoice_id",
    ):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
    assert params["note_override"].default is None


def test_signatures_match_the_interface_contract() -> None:
    assert list(inspect.signature(ai.extract_contract_rules).parameters)[:2] == [
        "file_bytes",
        "filename",
    ]
    assert list(inspect.signature(ai.evaluate_payment_compliance).parameters)[:6] == [
        "contract",
        "milestone",
        "captured_amount",
        "captured_currency",
        "captured_date",
        "payer_note",
    ]


@pytest.mark.asyncio
async def test_extract_then_evaluate_end_to_end_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    contract = await ai.extract_contract_rules(
        (CONTRACTS / "Acme_Standard.pdf").read_bytes(), "Acme_Standard.pdf"
    )
    assert isinstance(contract, ContractExtractionResult)

    result = await ai.evaluate_payment_compliance(
        contract, contract.milestones[0], Decimal("850.00"), "USD", date(2026, 10, 20)
    )
    assert isinstance(result, DiscrepancyEvaluationResult)
    assert result.total_remediation_amount == Decimal("200.00")


@pytest.mark.asyncio
async def test_bad_upload_raises_the_exported_error() -> None:
    with pytest.raises(ai.ContractParseError):
        await ai.extract_contract_rules(b"", "empty.pdf")
