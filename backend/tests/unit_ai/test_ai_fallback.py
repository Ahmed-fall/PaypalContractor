"""TST-07: model timeout / outage still returns a valid result, fast."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from backend.app.ai.auditor_agent import evaluate_payment_compliance
from backend.app.schemas.contracts import DiscrepancyType

from ._auditor_helpers import (
    ACME,
    ACME_CAPTURE,
    GOOD_ACME_PROSE,
    D,
    FakeAPIError,
    FakeWriter,
    milestone,
)


def assert_valid_template_result(r: object) -> None:
    assert r.discrepancy_type is DiscrepancyType.UNDERPAID_AND_LATE  # type: ignore[attr-defined]
    assert r.total_remediation_amount == D("200.00")  # type: ignore[attr-defined]
    assert ACME.penalty_clause_quote in r.paypal_invoice_detail_note  # type: ignore[attr-defined]
    assert "$200.00" in r.ai_audit_summary  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_timeout_error_falls_back_within_200ms() -> None:
    writer = FakeWriter(exc=TimeoutError())
    start = time.perf_counter()
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[writer]
    )
    assert time.perf_counter() - start < 0.2
    assert writer.calls == 1
    assert_valid_template_result(r)


@pytest.mark.asyncio
async def test_hanging_model_is_cut_off_by_timeout() -> None:
    writer = FakeWriter(GOOD_ACME_PROSE, delay=10.0)
    start = time.perf_counter()
    r = await evaluate_payment_compliance(
        ACME,
        milestone(ACME),
        **ACME_CAPTURE,
        writers=[writer],
        provider_timeout=0.1,
        budget_seconds=0.15,
    )
    assert time.perf_counter() - start < 0.4
    assert_valid_template_result(r)


def test_default_budget_caps_capture_latency_at_3s() -> None:
    from backend.app.ai.auditor_agent import (
        AUDIT_BUDGET_SECONDS,
        AUDIT_PROVIDER_TIMEOUT_SECONDS,
    )

    assert AUDIT_BUDGET_SECONDS <= 3.0
    assert AUDIT_PROVIDER_TIMEOUT_SECONDS < AUDIT_BUDGET_SECONDS


@pytest.mark.asyncio
async def test_hanging_model_with_default_budget_returns_within_3s() -> None:
    writer = FakeWriter(GOOD_ACME_PROSE, delay=30.0)
    backup = FakeWriter(GOOD_ACME_PROSE, delay=30.0)
    start = time.perf_counter()
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[writer, backup]
    )
    assert time.perf_counter() - start < 3.2
    assert_valid_template_result(r)


@pytest.mark.asyncio
async def test_overloaded_models_fall_back_after_one_retry_each() -> None:
    primary = FakeWriter(exc=FakeAPIError(503, "high demand"))
    backup = FakeWriter(exc=FakeAPIError(503, "high demand"))
    r = await evaluate_payment_compliance(
        ACME,
        milestone(ACME),
        **ACME_CAPTURE,
        writers=[primary, backup],
        retry_delay=0.01,
    )
    assert (primary.calls, backup.calls) == (2, 2)
    assert_valid_template_result(r)


@pytest.mark.asyncio
async def test_transient_error_then_success_uses_model_prose() -> None:
    writer = FakeWriter(outcomes=[FakeAPIError(503), GOOD_ACME_PROSE])
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[writer], retry_delay=0.01
    )
    assert writer.calls == 2
    assert r.ai_audit_summary == GOOD_ACME_PROSE.ai_audit_summary


@pytest.mark.asyncio
async def test_malformed_model_output_falls_back() -> None:
    try:
        from backend.app.ai.auditor_agent import AuditProse

        AuditProse.model_validate_json("{not json")
    except ValidationError as exc:
        malformed = exc
    writer = FakeWriter(exc=malformed)
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[writer]
    )
    assert_valid_template_result(r)


@pytest.mark.asyncio
async def test_no_api_key_uses_template_instantly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    start = time.perf_counter()
    r = await evaluate_payment_compliance(ACME, milestone(ACME), **ACME_CAPTURE)
    assert time.perf_counter() - start < 0.2
    assert_valid_template_result(r)
