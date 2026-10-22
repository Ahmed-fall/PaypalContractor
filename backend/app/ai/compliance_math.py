"""Deterministic Decimal shortfall / late-fee calculator.

The single source of truth for money in the audit path. The LLM never computes or
overrides any value produced here. Floats are rejected; all results are quantized to
``0.01`` with ``ROUND_HALF_UP``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from ..schemas.contracts import DiscrepancyStatus, DiscrepancyType

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class DiscrepancyMath:
    discrepancy_type: DiscrepancyType
    discrepancy_status: DiscrepancyStatus
    is_compliant: bool
    expected_amount: Decimal
    captured_amount: Decimal
    captured_currency: str | None
    shortfall_amount: Decimal
    penalty_amount: Decimal
    total_remediation_amount: Decimal


def _money(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def calculate_discrepancy_math(
    *,
    expected_amount: Decimal,
    captured_amount: Decimal,
    due_date: date,
    captured_date: date,
    grace_period_days: int,
    late_fee_percentage: Decimal,
    allow_partial_payments: bool,
    contract_currency: str = "USD",
    captured_currency: str | None = None,
) -> DiscrepancyMath:
    expected = _money(expected_amount, "expected_amount")
    captured = _money(captured_amount, "captured_amount")
    if not isinstance(late_fee_percentage, Decimal):
        raise TypeError("late_fee_percentage must be Decimal")
    if late_fee_percentage < 0:
        raise ValueError("late_fee_percentage must not be negative")

    if captured_currency is not None and captured_currency != contract_currency:
        # No FX conversion: flag only, never remediate with mixed-currency numbers.
        return DiscrepancyMath(
            discrepancy_type=DiscrepancyType.CURRENCY_MISMATCH,
            discrepancy_status=DiscrepancyStatus.PENDING_APPROVAL,
            is_compliant=False,
            expected_amount=expected,
            captured_amount=captured,
            captured_currency=captured_currency,
            shortfall_amount=_ZERO,
            penalty_amount=_ZERO,
            total_remediation_amount=_ZERO,
        )

    shortfall = max(_ZERO, expected - captured)
    is_late = captured_date > due_date + timedelta(days=grace_period_days)
    breached_partial = shortfall > 0 and not allow_partial_payments

    penalty = _ZERO
    if is_late or breached_partial:
        penalty = (expected * late_fee_percentage / _HUNDRED).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
    total = shortfall + penalty

    if shortfall > 0 and is_late:
        dtype = DiscrepancyType.UNDERPAID_AND_LATE
    elif shortfall > 0:
        dtype = DiscrepancyType.UNDERPAID
    elif is_late:
        dtype = DiscrepancyType.LATE_PAYMENT
    else:
        dtype = DiscrepancyType.COMPLIANT

    compliant = dtype is DiscrepancyType.COMPLIANT
    return DiscrepancyMath(
        discrepancy_type=dtype,
        discrepancy_status=(
            DiscrepancyStatus.NONE if compliant else DiscrepancyStatus.PENDING_APPROVAL
        ),
        is_compliant=compliant,
        expected_amount=expected,
        captured_amount=captured,
        captured_currency=captured_currency,
        shortfall_amount=shortfall,
        penalty_amount=penalty,
        total_remediation_amount=total,
    )
