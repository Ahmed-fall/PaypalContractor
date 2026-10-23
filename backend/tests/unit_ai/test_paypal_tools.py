"""PayPal Agent Toolkit–compatible tool models, builders and declarations."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from backend.app.ai.auditor_agent import (
    FOOTER_AMOUNT_PREFIX,
    FOOTER_CLAUSE_PREFIX,
    evaluate_payment_compliance,
)
from backend.app.ai.paypal_tools import (
    PAYPAL_BUSINESS_NAME_MAX,
    PAYPAL_REFERENCE_MAX,
    BillingInfo,
    CreateInvoiceParameters,
    GenerateInvoiceQrCodeParameters,
    InvoiceInvariantError,
    InvoiceItem,
    InvoiceNoteTooLong,
    Money,
    NothingToInvoice,
    PrimaryRecipient,
    SendInvoiceParameters,
    SendInvoiceReminderParameters,
    build_create_invoice_params,
    engine_note_footer,
    tool_declarations,
)
from backend.app.schemas.contracts import (
    ContractExtractionResult,
    DiscrepancyEvaluationResult,
)

from ._auditor_helpers import ACME, ACME_CAPTURE, GLOBEX, INITECH, D, milestone
from .test_paypal_invoice_limits import (
    PAYPAL_ITEM_DESCRIPTION_MAX,
    PAYPAL_ITEM_NAME_MAX,
    PAYPAL_MONEY_VALUE_RE,
    PAYPAL_NOTE_MAX,
)

SCENARIOS = [
    (ACME, "MS-01", "850.00", date(2026, 10, 20)),
    (GLOBEX, "MS-01", "2500.00", date(2026, 11, 4)),
    (INITECH, "MS-02", "1500.00", date(2026, 11, 9)),
]
SCENARIO_IDS = ["acme-underpaid-late", "globex-late", "initech-partial"]


async def acme_evaluation() -> DiscrepancyEvaluationResult:
    return await evaluate_payment_compliance(
        ACME, milestone(ACME), **ACME_CAPTURE, writers=[]
    )


# --- builder ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acme_create_params() -> None:
    evaluation = await acme_evaluation()
    params = build_create_invoice_params(ACME, milestone(ACME), evaluation)

    assert params.currency_code == "USD"
    assert params.reference == "MS-01"
    assert params.note == evaluation.paypal_invoice_detail_note
    billing = params.primary_recipients[0].billing_info
    assert billing is not None
    assert billing.email_address == "sb-buyer@personal.example.com"
    assert billing.business_name == "Acme Corp"
    assert [
        (i.name, i.unit_amount.value, i.unit_amount.currency_code, i.quantity)
        for i in params.items
    ] == [
        ("Milestone shortfall - MS-01", "150.00", "USD", "1"),
        ("Late fee (5.00%) - MS-01", "50.00", "USD", "1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract", "code", "paid", "when"), SCENARIOS, ids=SCENARIO_IDS
)
async def test_item_values_sum_to_engine_total(
    contract: ContractExtractionResult, code: str, paid: str, when: date
) -> None:
    ms = milestone(contract, code)
    evaluation = await evaluate_payment_compliance(
        contract, ms, D(paid), "USD", when, writers=[]
    )
    params = build_create_invoice_params(contract, ms, evaluation)
    total = sum((Decimal(i.unit_amount.value) for i in params.items), Decimal(0))
    assert total == evaluation.total_remediation_amount


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract", "code", "paid", "currency", "when"),
    [
        (INITECH, "MS-01", "1200.00", "USD", date(2026, 10, 10)),
        (GLOBEX, "MS-01", "2300.00", "EUR", date(2026, 11, 2)),
    ],
    ids=["compliant", "currency-mismatch"],
)
async def test_zero_total_is_refused(
    contract: ContractExtractionResult, code: str, paid: str, currency: str, when: date
) -> None:
    ms = milestone(contract, code)
    evaluation = await evaluate_payment_compliance(
        contract, ms, D(paid), currency, when, writers=[]
    )
    assert evaluation.total_remediation_amount == Decimal("0.00")
    with pytest.raises(NothingToInvoice):
        build_create_invoice_params(contract, ms, evaluation)


@pytest.mark.asyncio
async def test_params_respect_paypal_limits() -> None:
    params = build_create_invoice_params(ACME, milestone(ACME), await acme_evaluation())
    assert params.note is not None and len(params.note) <= PAYPAL_NOTE_MAX
    for item in params.items:
        assert len(item.name) <= PAYPAL_ITEM_NAME_MAX
        assert item.description is not None
        assert len(item.description) <= PAYPAL_ITEM_DESCRIPTION_MAX
        assert PAYPAL_MONEY_VALUE_RE.fullmatch(item.unit_amount.value)


# --- note footer and human override ----------------------------------------------------


@pytest.mark.asyncio
async def test_engine_note_footer_extracts_clause_and_amount() -> None:
    evaluation = await acme_evaluation()
    footer = engine_note_footer(evaluation)
    assert footer.startswith(f'{FOOTER_CLAUSE_PREFIX}"Section 4.2:')
    assert footer.endswith(
        f"{FOOTER_AMOUNT_PREFIX}$200.00 (shortfall $150.00 + late fee $50.00)"
    )
    no_footer = evaluation.model_copy(update={"paypal_invoice_detail_note": "plain"})
    assert engine_note_footer(no_footer) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract", "code", "paid", "when"), SCENARIOS, ids=SCENARIO_IDS
)
async def test_footer_found_for_every_scenario(
    contract: ContractExtractionResult, code: str, paid: str, when: date
) -> None:
    """Footer wording lives in auditor_agent only; the parser must track it."""
    evaluation = await evaluate_payment_compliance(
        contract, milestone(contract, code), D(paid), "USD", when, writers=[]
    )
    footer = engine_note_footer(evaluation)
    assert footer.startswith(FOOTER_CLAUSE_PREFIX)
    assert f"{FOOTER_AMOUNT_PREFIX}" in footer
    assert evaluation.paypal_invoice_detail_note.endswith(footer)


@pytest.mark.asyncio
async def test_footer_ignores_marker_lines_written_in_the_body() -> None:
    """An AI-written body may contain marker-like lines; only the real footer counts."""
    evaluation = await acme_evaluation()
    real_footer = engine_note_footer(evaluation)
    tricky = (
        f"Body text.\n\n{FOOTER_CLAUSE_PREFIX}fake\nmore body\n\n"
        f"{FOOTER_AMOUNT_PREFIX}$0.00\n\n{real_footer}"
    )
    assert (
        engine_note_footer(
            evaluation.model_copy(update={"paypal_invoice_detail_note": tricky})
        )
        == real_footer
    )


@pytest.mark.asyncio
async def test_footer_without_clause_starts_at_amount_line() -> None:
    evaluation = (await acme_evaluation()).model_copy(
        update={
            "violated_clause_quote": "",
            "paypal_invoice_detail_note": (
                f"Body.\n{FOOTER_CLAUSE_PREFIX}fake\nmore body\n\n"
                f"{FOOTER_AMOUNT_PREFIX}$200.00 (shortfall $150.00 + late fee $50.00)"
            ),
        }
    )
    assert engine_note_footer(evaluation) == (
        f"{FOOTER_AMOUNT_PREFIX}$200.00 (shortfall $150.00 + late fee $50.00)"
    )


@pytest.mark.asyncio
async def test_note_override_replaces_body_but_keeps_footer() -> None:
    evaluation = await acme_evaluation()
    params = build_create_invoice_params(
        ACME, milestone(ACME), evaluation, note_override="Please settle by Friday."
    )
    assert params.note is not None
    assert params.note.startswith("Please settle by Friday.")
    assert params.note.endswith(engine_note_footer(evaluation))
    assert "This supplemental invoice relates to" not in params.note


@pytest.mark.asyncio
async def test_override_with_empty_footer_is_used_alone() -> None:
    evaluation = (await acme_evaluation()).model_copy(
        update={"paypal_invoice_detail_note": "plain note", "violated_clause_quote": ""}
    )
    params = build_create_invoice_params(
        ACME, milestone(ACME), evaluation, note_override="  Pay by Friday.  "
    )
    assert params.note == "Pay by Friday."


@pytest.mark.asyncio
async def test_override_at_exact_paypal_limit_is_accepted() -> None:
    evaluation = await acme_evaluation()
    room = PAYPAL_NOTE_MAX - len(engine_note_footer(evaluation)) - 2
    params = build_create_invoice_params(
        ACME, milestone(ACME), evaluation, note_override="x" * room
    )
    assert params.note is not None and len(params.note) == PAYPAL_NOTE_MAX
    with pytest.raises(InvoiceNoteTooLong):
        build_create_invoice_params(
            ACME, milestone(ACME), evaluation, note_override="x" * (room + 1)
        )


# --- PayPal length limits on contract-derived fields -------------------------------------


@pytest.mark.asyncio
async def test_long_reference_and_business_name_are_clipped_to_paypal_limits() -> None:
    evaluation = await acme_evaluation()
    long_ms = milestone(ACME).model_copy(update={"milestone_code": "M" * 500})
    long_contract = ACME.model_copy(update={"client_name": "C" * 900})
    params = build_create_invoice_params(long_contract, long_ms, evaluation)
    billing = params.primary_recipients[0].billing_info
    assert billing is not None and billing.business_name is not None
    assert params.reference is not None
    assert len(params.reference) <= PAYPAL_REFERENCE_MAX == 120
    assert len(billing.business_name) <= PAYPAL_BUSINESS_NAME_MAX == 300


# --- builder re-checks its own money invariants ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "breakage", ["sum-mismatch", "non-positive-item", "foreign-item-currency"]
)
async def test_builder_rejects_inconsistent_evaluations(breakage: str) -> None:
    evaluation = await acme_evaluation()
    items = [i.model_copy() for i in evaluation.remediation_line_items]
    if breakage == "sum-mismatch":
        items[1] = items[1].model_copy(update={"unit_amount_value": Decimal("49.99")})
    elif breakage == "non-positive-item":
        items[0] = items[0].model_copy(update={"unit_amount_value": Decimal("0.00")})
        items[1] = items[1].model_copy(update={"unit_amount_value": Decimal("200.00")})
    else:
        items[1] = items[1].model_copy(update={"currency_code": "EUR"})
    broken = evaluation.model_copy(update={"remediation_line_items": items})
    with pytest.raises(InvoiceInvariantError):
        build_create_invoice_params(ACME, milestone(ACME), broken)


@pytest.mark.asyncio
async def test_blank_override_is_ignored() -> None:
    evaluation = await acme_evaluation()
    params = build_create_invoice_params(
        ACME, milestone(ACME), evaluation, note_override="   "
    )
    assert params.note == evaluation.paypal_invoice_detail_note


@pytest.mark.asyncio
async def test_oversized_override_is_rejected() -> None:
    with pytest.raises(InvoiceNoteTooLong):
        build_create_invoice_params(
            ACME, milestone(ACME), await acme_evaluation(), note_override="x" * 4000
        )


# --- parity with paypal-agent-toolkit 1.11.0 -------------------------------------------

UPSTREAM_1_11_0: dict[type[BaseModel], set[str]] = {
    CreateInvoiceParameters: {
        "currency_code",
        "invoice_number",
        "invoice_date",
        "reference",
        "note",
        "invoicer_business_name",
        "invoicer_given_name",
        "invoicer_surname",
        "invoicer_email_address",
        "invoicer_tax_id",
        "invoicer_address_line_1",
        "invoicer_address_line_2",
        "invoicer_city",
        "invoicer_state",
        "invoicer_postal_code",
        "invoicer_country_code",
        "primary_recipients",
        "items",
        "allow_tip",
        "theme_color",
        "shipping_cost",
        "enable_pay_by_bank",
        "pay_by_bank_exclusive_above_threshold",
        "allow_partial_payment",
        "minimum_partial_payment_amount",
    },
    InvoiceItem: {
        "name",
        "description",
        "quantity",
        "unit_amount",
        "tax",
        "discount",
        "item_date",
        "unit_of_measure",
    },
    BillingInfo: {
        "business_name",
        "name",
        "address",
        "email_address",
        "phones",
        "additional_info",
        "language",
    },
    PrimaryRecipient: {"billing_info", "shipping_info"},
    SendInvoiceParameters: {
        "invoice_id",
        "note",
        "send_to_recipient",
        "additional_recipients",
    },
    SendInvoiceReminderParameters: {
        "invoice_id",
        "subject",
        "note",
        "additional_recipients",
    },
}


@pytest.mark.parametrize("model", list(UPSTREAM_1_11_0), ids=lambda m: m.__name__)
def test_field_names_match_agent_toolkit_1_11_0(model: type[BaseModel]) -> None:
    assert set(model.model_fields) <= UPSTREAM_1_11_0[model]


def test_exact_field_sets_for_money_and_qr_code() -> None:
    assert set(Money.model_fields) == {"currency_code", "value"}
    assert set(GenerateInvoiceQrCodeParameters.model_fields) == {
        "invoice_id",
        "width",
        "height",
    }


# --- Gemini tool declarations ------------------------------------------------------------


def declaration(name: str) -> dict[str, Any]:
    found = [d for d in tool_declarations() if d["name"] == name]
    assert len(found) == 1, name
    return found[0]


def test_declarations_cover_the_four_toolkit_tools() -> None:
    assert [d["name"] for d in tool_declarations()] == [
        "create_invoice",
        "send_invoice",
        "generate_invoice_qr_code",
        "send_invoice_reminder",
    ]
    for d in tool_declarations():
        assert isinstance(d["description"], str) and d["description"]


def test_declaration_schemas_come_from_models() -> None:
    create = declaration("create_invoice")["parameters"]
    assert {"currency_code", "primary_recipients", "items"} <= set(create["required"])
    unit_amount = create["properties"]["items"]["items"]["properties"]["unit_amount"]
    assert {"currency_code", "value"} <= set(unit_amount["properties"])
    qr = declaration("generate_invoice_qr_code")["parameters"]
    assert "invoice_id" in qr["required"]


def test_declaration_schemas_are_flat() -> None:
    dumped = json.dumps(tool_declarations())
    assert "$ref" not in dumped
    assert "$defs" not in dumped


def test_declarations_never_offer_destructive_tools() -> None:
    names = {d["name"] for d in tool_declarations()}
    assert not names & {
        "delete_invoice",
        "cancel_sent_invoice",
        "record_refund_for_invoice",
    }
