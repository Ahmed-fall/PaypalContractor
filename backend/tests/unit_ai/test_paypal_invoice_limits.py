"""Engine invoice output vs. PayPal's *official* Invoicing v2 limits.

``test_compliance_auditor.py`` checks the stricter interface limits from
``contracts.py`` (120/500/1500). This file checks PayPal's own limits and formats,
read from https://developer.paypal.com/api/invoicing/v2/schema.json on 2026-10-23,
so a future change to the shared interface cannot silently break PayPal.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from backend.app.ai.auditor_agent import evaluate_payment_compliance
from backend.app.schemas.contracts import (
    ContractExtractionResult,
    DiscrepancyEvaluationResult,
)

from ._auditor_helpers import ACME, ACME_CAPTURE, GLOBEX, INITECH, D, milestone

PAYPAL_NOTE_MAX = 4000  # detail.note
PAYPAL_ITEM_NAME_MAX = 200  # items[].name
PAYPAL_ITEM_DESCRIPTION_MAX = 1000  # items[].description
PAYPAL_QUANTITY_RE = re.compile(r"^\d+(.\d{1,5})?$")  # items[].quantity
PAYPAL_MONEY_VALUE_RE = re.compile(
    r"^((-?[0-9]+)|(-?([0-9]+)?[.][0-9]+))$"
)  # money.value


def assert_fits_paypal(
    result: DiscrepancyEvaluationResult, contract: ContractExtractionResult
) -> None:
    assert len(result.paypal_invoice_detail_note) <= PAYPAL_NOTE_MAX
    for item in result.remediation_line_items:
        assert len(item.name) <= PAYPAL_ITEM_NAME_MAX
        assert len(item.description) <= PAYPAL_ITEM_DESCRIPTION_MAX
        assert PAYPAL_QUANTITY_RE.fullmatch(item.quantity)
        assert PAYPAL_MONEY_VALUE_RE.fullmatch(f"{item.unit_amount_value:.2f}")
        assert item.currency_code == contract.currency


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract", "code", "paid", "when"),
    [
        (ACME, "MS-01", "850.00", date(2026, 10, 20)),
        (GLOBEX, "MS-01", "2500.00", date(2026, 11, 4)),
        (INITECH, "MS-02", "1500.00", date(2026, 11, 9)),
    ],
    ids=["acme-underpaid-late", "globex-late", "initech-partial"],
)
async def test_fixture_results_fit_paypal_limits(
    contract: ContractExtractionResult, code: str, paid: str, when: date
) -> None:
    result = await evaluate_payment_compliance(
        contract, milestone(contract, code), D(paid), "USD", when, writers=[]
    )
    assert result.remediation_line_items, "scenario must produce invoice lines"
    assert_fits_paypal(result, contract)


@pytest.mark.asyncio
async def test_oversized_contract_text_still_fits_paypal_limits() -> None:
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
    result = await evaluate_payment_compliance(
        long_contract, long_ms, **ACME_CAPTURE, writers=[]
    )
    assert_fits_paypal(result, long_contract)


@pytest.mark.parametrize(
    ("pattern", "good", "bad"),
    [
        (PAYPAL_QUANTITY_RE, ["1", "2.5", "10.12345"], ["", "-1", "1.123456", "x"]),
        (
            PAYPAL_MONEY_VALUE_RE,
            ["150.00", "0.50", "200"],
            ["", "1,000.00", "$5", "abc"],
        ),
    ],
    ids=["quantity", "money-value"],
)
def test_paypal_patterns_reject_bad_values(
    pattern: re.Pattern[str], good: list[str], bad: list[str]
) -> None:
    """Guards the guards: the checks above must be able to fail."""
    assert all(pattern.fullmatch(v) for v in good)
    assert not any(pattern.fullmatch(v) for v in bad)
