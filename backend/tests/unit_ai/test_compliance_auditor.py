"""REQ-AI-02 / TST-05: hybrid evaluation, locked math plus clause-citing prose."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from backend.app.ai.auditor_agent import (
    INVOICE_NOTE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    AuditProse,
    evaluate_payment_compliance,
)
from backend.app.schemas.contracts import (
    DiscrepancyEvaluationResult,
    DiscrepancyStatus,
    DiscrepancyType,
)

from ._auditor_helpers import (
    ACME,
    ACME_CAPTURE,
    GLOBEX,
    GOOD_ACME_PROSE,
    INITECH,
    D,
    FakeWriter,
    milestone,
)


def assert_well_formed(r: DiscrepancyEvaluationResult) -> None:
    assert r.total_remediation_amount == r.shortfall_amount + r.penalty_amount
    assert (
        sum((i.unit_amount_value for i in r.remediation_line_items), D("0.00"))
        == r.total_remediation_amount
    )
    assert len(r.ai_audit_summary) <= SUMMARY_MAX_CHARS
    assert len(r.paypal_invoice_detail_note) <= INVOICE_NOTE_MAX_CHARS
    for item in r.remediation_line_items:
        assert len(item.name) <= 120 and len(item.description) <= 500
        assert item.quantity == "1"
        assert item.unit_amount_value > 0
    if r.total_remediation_amount > 0:
        assert r.violated_clause_quote in r.paypal_invoice_detail_note
        assert f"{r.total_remediation_amount:,.2f}" in r.paypal_invoice_detail_note
        assert f"{r.total_remediation_amount:,.2f}" in r.ai_audit_summary


# --- the three fixture scenarios (template prose, no model) ----------------------------


@pytest.mark.asyncio
async def test_acme_headline_underpaid_and_late() -> None:
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[]
    )
    assert r.discrepancy_type is DiscrepancyType.UNDERPAID_AND_LATE
    assert r.discrepancy_status is DiscrepancyStatus.PENDING_APPROVAL
    assert not r.is_compliant
    assert (r.expected_amount, r.captured_amount) == (D("1000.00"), D("850.00"))
    assert (r.shortfall_amount, r.penalty_amount, r.total_remediation_amount) == (
        D("150.00"),
        D("50.00"),
        D("200.00"),
    )
    assert r.captured_currency == "USD"
    assert r.violated_clause_quote == ACME.penalty_clause_quote
    assert [(i.name, i.unit_amount_value) for i in r.remediation_line_items] == [
        ("Milestone shortfall - MS-01", D("150.00")),
        ("Late fee (5.00%) - MS-01", D("50.00")),
    ]
    assert "Section 3.1" in r.remediation_line_items[0].description
    assert "Section 4.2" in r.remediation_line_items[1].description
    assert all(i.currency_code == "USD" for i in r.remediation_line_items)
    assert_well_formed(r)


@pytest.mark.asyncio
async def test_globex_paid_in_full_but_late() -> None:
    r = await evaluate_payment_compliance(
        GLOBEX, milestone(GLOBEX), D("2500.00"), "USD", date(2026, 11, 4), writers=[]
    )
    assert r.discrepancy_type is DiscrepancyType.LATE_PAYMENT
    assert (r.shortfall_amount, r.penalty_amount, r.total_remediation_amount) == (
        D("0.00"),
        D("175.00"),
        D("175.00"),
    )
    assert [i.name for i in r.remediation_line_items] == ["Late fee (7.00%) - MS-01"]
    assert r.violated_clause_quote == GLOBEX.penalty_clause_quote
    assert "3 days" in r.ai_audit_summary
    assert_well_formed(r)


@pytest.mark.asyncio
async def test_initech_partial_payment_allowed_is_shortfall_only() -> None:
    ms2 = milestone(INITECH, "MS-02")
    r = await evaluate_payment_compliance(
        INITECH, ms2, D("1500.00"), "USD", date(2026, 11, 9), writers=[]
    )
    assert r.discrepancy_type is DiscrepancyType.UNDERPAID
    assert (r.shortfall_amount, r.penalty_amount, r.total_remediation_amount) == (
        D("300.00"),
        D("0.00"),
        D("300.00"),
    )
    assert r.violated_clause_quote == ms2.source_clause_quote
    assert [i.name for i in r.remediation_line_items] == ["Milestone shortfall - MS-02"]
    assert_well_formed(r)


@pytest.mark.asyncio
async def test_compliant_payment_makes_no_model_call() -> None:
    writer = FakeWriter(GOOD_ACME_PROSE)
    r = await evaluate_payment_compliance(
        INITECH,
        milestone(INITECH),
        D("1200.00"),
        "USD",
        date(2026, 10, 10),
        writers=[writer],
    )
    assert r.is_compliant
    assert r.discrepancy_type is DiscrepancyType.COMPLIANT
    assert r.discrepancy_status is DiscrepancyStatus.NONE
    assert r.remediation_line_items == []
    assert r.paypal_invoice_detail_note == ""
    assert r.violated_clause_quote == ""
    assert "MS-01" in r.ai_audit_summary
    assert writer.calls == 0
    assert_well_formed(r)


@pytest.mark.asyncio
async def test_currency_mismatch_is_flagged_without_invoice_lines() -> None:
    r = await evaluate_payment_compliance(
        GLOBEX, milestone(GLOBEX), D("2300.00"), "eur", date(2026, 11, 2), writers=[]
    )
    assert r.discrepancy_type is DiscrepancyType.CURRENCY_MISMATCH
    assert r.discrepancy_status is DiscrepancyStatus.PENDING_APPROVAL
    assert r.captured_currency == "EUR"
    assert r.total_remediation_amount == D("0.00")
    assert r.remediation_line_items == []
    assert "EUR" in r.ai_audit_summary and "USD" in r.ai_audit_summary
    assert r.violated_clause_quote == milestone(GLOBEX).source_clause_quote
    assert_well_formed(r)


@pytest.mark.asyncio
async def test_datetime_capture_is_accepted() -> None:
    when = datetime(2026, 10, 20, 14, 22, 10, tzinfo=UTC)
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), D("850.00"), "USD", when, writers=[]
    )
    assert r.total_remediation_amount == D("200.00")


@pytest.mark.asyncio
async def test_on_time_overpayment_is_compliant() -> None:
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), D("1200.00"), "USD", date(2026, 10, 15), writers=[]
    )
    assert r.is_compliant and r.total_remediation_amount == D("0.00")


# --- model-written prose ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_model_prose_is_used_and_footer_is_appended() -> None:
    writer = FakeWriter(GOOD_ACME_PROSE)
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[writer]
    )
    assert writer.calls == 1
    assert r.ai_audit_summary == GOOD_ACME_PROSE.ai_audit_summary
    assert r.paypal_invoice_detail_note.startswith(
        GOOD_ACME_PROSE.paypal_invoice_detail_note
    )
    assert ACME.penalty_clause_quote in r.paypal_invoice_detail_note
    assert "Amount due: $200.00" in r.paypal_invoice_detail_note
    assert_well_formed(r)


@pytest.mark.asyncio
async def test_prompt_carries_locked_figures() -> None:
    writer = FakeWriter(GOOD_ACME_PROSE)
    await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[writer]
    )
    prompt = writer.prompts[0]
    for fact in (
        "total_remediation_amount: $200.00",
        "shortfall_amount: $150.00",
        "penalty_amount: $50.00",
        "discrepancy_type: UNDERPAID_AND_LATE",
        "days_after_due_date: 5",
    ):
        assert fact in prompt


@pytest.mark.parametrize(
    "prose",
    [
        AuditProse(
            ai_audit_summary="Client owes $180.00 for MS-01.",
            paypal_invoice_detail_note="Pay $180.00.",
        ),
        AuditProse(
            ai_audit_summary="A balance is outstanding for MS-01.",
            paypal_invoice_detail_note="See below.",
        ),
        AuditProse(
            ai_audit_summary="x" * 601 + " $200.00",
            paypal_invoice_detail_note="$200.00 due.",
        ),
        AuditProse(
            ai_audit_summary="$200.00 is due.",
            paypal_invoice_detail_note="$200.00 due. " + "y" * 1300,
        ),
        AuditProse(
            ai_audit_summary="$200.00 is due, i.e. 200.01 rounded.",
            paypal_invoice_detail_note="$200.00.",
        ),
        AuditProse(ai_audit_summary="", paypal_invoice_detail_note=""),
        AuditProse(
            ai_audit_summary="$200.00 is due.",
            paypal_invoice_detail_note="Please remit the total_remediation_amount of $200.00.",
        ),
    ],
    ids=[
        "wrong-amount",
        "no-total",
        "summary-too-long",
        "note-too-long",
        "bare-wrong-number",
        "empty",
        "field-name",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_model_prose_falls_back_to_template(prose: AuditProse) -> None:
    writer = FakeWriter(prose)
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[writer]
    )
    assert r.ai_audit_summary != prose.ai_audit_summary
    assert (r.shortfall_amount, r.penalty_amount, r.total_remediation_amount) == (
        D("150.00"),
        D("50.00"),
        D("200.00"),
    )
    assert_well_formed(r)


@pytest.mark.asyncio
async def test_percent_and_dates_in_prose_are_not_mistaken_for_money() -> None:
    prose = AuditProse(
        ai_audit_summary="Paid $850.00 on 2026-10-20, 5 days late; Section 4.2 applies a 5.00% fee. Due: $200.00.",
        paypal_invoice_detail_note="Per Section 4.2 (5.00% fee), $200.00 is due for MS-01.",
    )
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[FakeWriter(prose)]
    )
    assert r.ai_audit_summary == prose.ai_audit_summary


# --- limits and robustness --------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_titles_and_clauses_stay_within_paypal_limits() -> None:
    long_ms = milestone(ACME).model_copy(
        update={
            "title": "Phase " * 60,
            "source_clause_quote": "Section 3.1: " + "z" * 900,
        }
    )
    long_contract = ACME.model_copy(
        update={
            "penalty_clause_quote": "Section 4.2: " + "w" * 2000,
            "contract_title": "T" * 400,
        }
    )
    r = await evaluate_payment_compliance(
        long_contract, long_ms, **ACME_CAPTURE, writers=[]
    )
    assert len(r.paypal_invoice_detail_note) <= INVOICE_NOTE_MAX_CHARS
    assert len(r.ai_audit_summary) <= SUMMARY_MAX_CHARS
    for item in r.remediation_line_items:
        assert len(item.name) <= 120 and len(item.description) <= 500
    assert "$200.00" in r.paypal_invoice_detail_note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "captured", ["0.00", "1.00", "500.00", "999.99", "1000.00", "1500.00"]
)
@pytest.mark.parametrize(
    "captured_on",
    [date(2026, 10, 1), date(2026, 10, 18), date(2026, 10, 19), date(2026, 12, 31)],
)
async def test_line_items_always_reconcile(captured: str, captured_on: date) -> None:
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), D(captured), "USD", captured_on, writers=[]
    )
    assert_well_formed(r)


@pytest.mark.asyncio
async def test_non_usd_contract_formats_currency_code() -> None:
    eur_contract = ACME.model_copy(update={"currency": "EUR"})
    r = await evaluate_payment_compliance(
        eur_contract,
        milestone(ACME),
        D("850.00"),
        "EUR",
        date(2026, 10, 20),
        writers=[],
    )
    assert "EUR 200.00" in r.ai_audit_summary
    assert all(i.currency_code == "EUR" for i in r.remediation_line_items)


@pytest.mark.asyncio
async def test_result_is_a_valid_canonical_model() -> None:
    r = await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[]
    )
    assert DiscrepancyEvaluationResult.model_validate(r.model_dump()) == r
    assert isinstance(r.total_remediation_amount, Decimal)
