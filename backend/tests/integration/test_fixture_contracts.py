"""Each shared/fixtures/json artifact must validate against contracts.py."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.app.schemas import contracts as c

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "json"

LEDGERS = (
    "acme_ledger.json",
    "globex_ledger.json",
    "globex_currency_mismatch.json",
    "initech_ledger.json",
)

CASES: dict[str, type] = {
    "acme_extract.json": c.ContractExtractResponse,
    "globex_extract.json": c.ContractExtractResponse,
    "initech_extract.json": c.ContractExtractResponse,
    "acme_ledger.json": c.LedgerResponse,
    "globex_ledger.json": c.LedgerResponse,
    "globex_currency_mismatch.json": c.LedgerResponse,
    "initech_ledger.json": c.LedgerResponse,
    "acme_simulate_request.json": c.SimulatorPaymentRequest,
    "acme_simulate_response.json": c.SimulatorPaymentResponse,
    "acme_remediation_response.json": c.RemediationResponse,
}


def _load_ledger(name: str) -> c.LedgerResponse:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return c.LedgerResponse.model_validate(payload)


@pytest.mark.parametrize("filename,model", CASES.items())
def test_fixture_conforms_to_schema(filename: str, model: type) -> None:
    payload = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
    reencoded = json.loads(model.model_validate(payload).model_dump_json())
    for key in ("expected_amount", "shortfall_amount", "penalty_amount", "total_remediation_amount"):
        if key in reencoded:
            assert isinstance(reencoded[key], str)


@pytest.mark.parametrize("name", LEDGERS)
def test_ledger_row_math_invariant(name: str) -> None:
    for row in _load_ledger(name).rows:
        assert row.total_remediation_amount == row.shortfall_amount + row.penalty_amount
        assert not isinstance(row.expected_amount, float)
        if row.captured_amount is not None and row.discrepancy_type != c.DiscrepancyType.CURRENCY_MISMATCH:
            assert row.shortfall_amount == max(
                Decimal("0.00"), row.expected_amount - row.captured_amount
            )


@pytest.mark.parametrize("name", LEDGERS)
def test_ledger_summary_matches_rows(name: str) -> None:
    ledger = _load_ledger(name)
    s = ledger.summary
    assert s.total_expected == sum(r.expected_amount for r in ledger.rows)
    assert s.total_shortfall == sum(r.shortfall_amount for r in ledger.rows)
    assert s.total_penalties == sum(r.penalty_amount for r in ledger.rows)
    assert s.total_captured == sum(
        (r.captured_amount or Decimal("0.00"))
        for r in ledger.rows
        if r.captured_currency in (None, r.currency)
    )
