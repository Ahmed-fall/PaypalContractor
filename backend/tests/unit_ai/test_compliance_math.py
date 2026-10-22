"""TST-05: deterministic Decimal math, float-drift prevention, boundary cases."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import pytest

from backend.app.ai.compliance_math import DiscrepancyMath, calculate_discrepancy_math
from backend.app.schemas.contracts import DiscrepancyStatus, DiscrepancyType

DUE = date(2026, 10, 15)
D = Decimal


def calc(
    expected: str = "1000.00",
    captured: str = "1000.00",
    *,
    days_after_due: int = 0,
    grace: int = 3,
    fee: str = "5.00",
    partial: bool = False,
    contract_currency: str = "USD",
    captured_currency: Optional[str] = None,
) -> DiscrepancyMath:
    return calculate_discrepancy_math(
        expected_amount=D(expected),
        captured_amount=D(captured),
        due_date=DUE,
        captured_date=DUE + timedelta(days=days_after_due),
        grace_period_days=grace,
        late_fee_percentage=D(fee),
        allow_partial_payments=partial,
        contract_currency=contract_currency,
        captured_currency=captured_currency,
    )


def test_headline_acme_underpaid_and_late() -> None:
    r = calc(captured="850.00", days_after_due=5)
    assert r.shortfall_amount == D("150.00")
    assert r.penalty_amount == D("50.00")
    assert r.total_remediation_amount == D("200.00")
    assert r.discrepancy_type is DiscrepancyType.UNDERPAID_AND_LATE
    assert r.discrepancy_status is DiscrepancyStatus.PENDING_APPROVAL
    assert not r.is_compliant


def test_globex_full_payment_late_no_grace() -> None:
    r = calc("2500.00", "2500.00", days_after_due=1, grace=0, fee="7.00")
    assert (r.shortfall_amount, r.penalty_amount) == (D("0.00"), D("175.00"))
    assert r.discrepancy_type is DiscrepancyType.LATE_PAYMENT


def test_initech_partial_allowed_on_time_is_shortfall_only() -> None:
    r = calc("1800.00", "1000.00", grace=5, partial=True)
    assert r.shortfall_amount == D("800.00")
    assert r.penalty_amount == D("0.00")
    assert r.total_remediation_amount == D("800.00")
    assert r.discrepancy_type is DiscrepancyType.UNDERPAID


def test_partial_forbidden_on_time_still_penalised() -> None:
    r = calc(captured="900.00", days_after_due=0, partial=False)
    assert r.penalty_amount == D("50.00")
    assert r.discrepancy_type is DiscrepancyType.UNDERPAID


def test_compliant_exact_and_on_time() -> None:
    r = calc()
    assert r.is_compliant
    assert r.discrepancy_type is DiscrepancyType.COMPLIANT
    assert r.discrepancy_status is DiscrepancyStatus.NONE
    assert r.total_remediation_amount == D("0.00")


@pytest.mark.parametrize(
    ("days_after_due", "late"),
    [(-5, False), (0, False), (2, False), (3, False), (4, True), (30, True)],
)
def test_grace_period_boundary(days_after_due: int, late: bool) -> None:
    r = calc(days_after_due=days_after_due, grace=3)
    assert (r.discrepancy_type is DiscrepancyType.LATE_PAYMENT) is late
    assert r.penalty_amount == (D("50.00") if late else D("0.00"))


def test_zero_grace_boundary() -> None:
    assert calc(days_after_due=0, grace=0).is_compliant
    assert calc(days_after_due=1, grace=0).discrepancy_type is DiscrepancyType.LATE_PAYMENT


def test_overpayment_on_time_is_compliant() -> None:
    r = calc(captured="1200.00")
    assert r.shortfall_amount == D("0.00")
    assert r.is_compliant


def test_overpayment_late_is_late_payment() -> None:
    r = calc(captured="1200.00", days_after_due=10)
    assert r.shortfall_amount == D("0.00")
    assert r.discrepancy_type is DiscrepancyType.LATE_PAYMENT


def test_zero_fee_late_has_no_penalty_but_is_flagged() -> None:
    r = calc(days_after_due=10, fee="0.00")
    assert r.penalty_amount == D("0.00")
    assert r.discrepancy_type is DiscrepancyType.LATE_PAYMENT


def test_float_drift_cents() -> None:
    # 333.33 * 7.25% = 24.166425 -> 24.17 (HALF_UP)
    r = calc("333.33", "333.33", days_after_due=9, fee="7.25")
    assert r.penalty_amount == D("24.17")


def test_round_half_up_not_bankers() -> None:
    # 10.10 * 5% = 0.505 -> 0.51 under HALF_UP (banker's rounding would give 0.50)
    r = calc("10.10", "10.10", days_after_due=9, fee="5.00")
    assert r.penalty_amount == D("0.51")


def test_unquantised_inputs_are_normalised() -> None:
    r = calc("1000", "850", days_after_due=5)
    assert r.shortfall_amount == D("150.00")
    assert str(r.total_remediation_amount) == "200.00"


def test_one_cent_short() -> None:
    r = calc(captured="999.99", partial=True)
    assert r.shortfall_amount == D("0.01")
    assert r.total_remediation_amount == D("0.01")


def test_currency_mismatch_is_flag_only() -> None:
    r = calc("2500.00", "2300.00", days_after_due=1, grace=0, fee="7.00", captured_currency="EUR")
    assert r.discrepancy_type is DiscrepancyType.CURRENCY_MISMATCH
    assert r.discrepancy_status is DiscrepancyStatus.PENDING_APPROVAL
    assert (r.shortfall_amount, r.penalty_amount, r.total_remediation_amount) == (
        D("0.00"),
        D("0.00"),
        D("0.00"),
    )
    assert r.captured_currency == "EUR"
    assert not r.is_compliant


def test_matching_currency_explicit_is_not_mismatch() -> None:
    assert calc(captured_currency="USD").is_compliant


def test_float_inputs_rejected() -> None:
    with pytest.raises(TypeError):
        calculate_discrepancy_math(
            expected_amount=1000.0,  # type: ignore[arg-type]
            captured_amount=D("1000.00"),
            due_date=DUE,
            captured_date=DUE,
            grace_period_days=0,
            late_fee_percentage=D("5.00"),
            allow_partial_payments=False,
        )


def test_negative_amount_rejected() -> None:
    with pytest.raises(ValueError):
        calc(captured="-1.00")


@pytest.mark.parametrize("expected", ["0.01", "1.00", "99.99", "1000.00", "12345.67", "999999.99"])
@pytest.mark.parametrize("captured_factor", ["0", "0.5", "1", "1.5"])
@pytest.mark.parametrize("days", [0, 4])
def test_total_invariant_across_grid(expected: str, captured_factor: str, days: int) -> None:
    """48 grid cases: total == shortfall + penalty exactly, all at 2 dp."""
    cap = (D(expected) * D(captured_factor)).quantize(D("0.01"))
    r = calc(expected, str(cap), days_after_due=days)
    assert r.total_remediation_amount == r.shortfall_amount + r.penalty_amount
    for v in (r.shortfall_amount, r.penalty_amount, r.total_remediation_amount):
        assert v == v.quantize(D("0.01"))
        assert v >= 0
