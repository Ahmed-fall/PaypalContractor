"""Chief ledger endpoint (WP-1.4): ``GET /ledger``.

Read is open (no demo key). Rows combine contract, milestone, capture,
discrepancy and remediation state; the status enum is the API one
(``NONE / PENDING_APPROVAL / BLOCKED / REMEDIATED``) mapped from the richer
DB lifecycle (A.5) — remediation changes status, never the audit reason.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, cast

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.database import get_db, utcnow
from ..db.models import Contract, Discrepancy, Milestone, PaypalCapture, RemediationInvoice
from ..schemas.contracts import (
    DiscrepancyStatus,
    DiscrepancyType,
    LedgerResponse,
    LedgerRow,
    LedgerSummary,
    LeashDecision,
    RemediationInvoiceRecord,
)

router = APIRouter(prefix="/ledger", tags=["ledger"])

_API_STATUS: dict[str, DiscrepancyStatus] = {
    "OPEN": DiscrepancyStatus.NONE,
    "NONE": DiscrepancyStatus.NONE,
    "PENDING_APPROVAL": DiscrepancyStatus.PENDING_APPROVAL,
    "BLOCKED": DiscrepancyStatus.BLOCKED,
    "REMEDIATED": DiscrepancyStatus.REMEDIATED,
    "SEND_FAILED": DiscrepancyStatus.NONE,
    "RECOVERED": DiscrepancyStatus.REMEDIATED,
}


def _api_status(db_status: str | None) -> DiscrepancyStatus:
    return _API_STATUS.get(db_status or "NONE", DiscrepancyStatus.NONE)


@router.get("", status_code=200)
def ledger(db: Session = Depends(get_db)) -> LedgerResponse:
    total_expected = Decimal("0.00")
    total_captured = Decimal("0.00")
    total_shortfall = Decimal("0.00")
    total_penalties = Decimal("0.00")
    total_remediation_invoiced = Decimal("0.00")

    rows: list[LedgerRow] = []
    milestones = db.scalars(
        select(Milestone).join(Contract, Contract.id == Milestone.contract_id)
    ).all()

    for milestone in milestones:
        capture = db.scalar(
            select(PaypalCapture)
            .where(PaypalCapture.milestone_id == milestone.id)
            .order_by(PaypalCapture.captured_at.desc())
            .limit(1)
        )
        discrepancy = db.scalar(
            select(Discrepancy).where(Discrepancy.milestone_id == milestone.id)
            .order_by(Discrepancy.created_at.desc())
            .limit(1)
        )
        invoice = (
            db.scalar(
                select(RemediationInvoice)
                .where(RemediationInvoice.discrepancy_id == discrepancy.id)
                .where(RemediationInvoice.status.in_(("SENT", "PAID")))
                .order_by(RemediationInvoice.attempt.desc())
                .limit(1)
            )
            if discrepancy
            else None
        )

        expected = milestone.expected_amount
        total_expected += expected
        if capture is not None:
            total_captured += capture.gross_amount
        if discrepancy is not None:
            total_shortfall += discrepancy.shortfall_amount
            total_penalties += discrepancy.penalty_amount
            if invoice is not None and invoice.status in ("SENT", "PAID"):
                total_remediation_invoiced += invoice.total_amount

        rows.append(
            LedgerRow(
                milestone_id=milestone.id,
                contract_id=milestone.contract_id,
                contract_title=_title(db, milestone.contract_id),
                client_name=_client(db, milestone.contract_id),
                client_paypal_email=_email(db, milestone.contract_id),
                milestone_code=milestone.code,
                milestone_title=milestone.title,
                expected_amount=expected,
                captured_amount=capture.gross_amount if capture else None,
                captured_currency=capture.currency if capture else None,
                shortfall_amount=discrepancy.shortfall_amount if discrepancy else Decimal("0.00"),
                penalty_amount=discrepancy.penalty_amount if discrepancy else Decimal("0.00"),
                total_remediation_amount=(
                    discrepancy.total_remediation_amount if discrepancy else Decimal("0.00")
                ),
                currency=capture.currency if capture else "USD",
                due_date=milestone.due_date,
                captured_at=capture.captured_at if capture else None,
                paypal_order_id=capture.paypal_order_id if capture else None,
                paypal_capture_id=capture.paypal_capture_id if capture else None,
                webhook_verified=capture.webhook_verified if capture else False,
                discrepancy_type=(
                    DiscrepancyType(discrepancy.discrepancy_type)
                    if discrepancy
                    else None
                ),
                discrepancy_status=(
                    _api_status(discrepancy.status) if discrepancy else DiscrepancyStatus.NONE
                ),
                discrepancy_id=discrepancy.id if discrepancy else None,
                violated_clause_quote=discrepancy.violated_clause_quote if discrepancy else None,
                ai_audit_summary=(
                    discrepancy.ai_audit_summary if discrepancy else None
                ),
                remediation_invoice=_invoice_record(invoice),
                leash_decision=_leash_decision(discrepancy),
                remediation_sent_by=(
                    cast("Literal['owner', 'leash-auto']", invoice.sent_by)
                    if invoice is not None and invoice.sent_by
                    else None
                ),
            )
        )

    return LedgerResponse(
        summary=LedgerSummary(
            total_expected=total_expected,
            total_captured=total_captured,
            total_shortfall=total_shortfall,
            total_penalties=total_penalties,
            total_remediation_invoiced=total_remediation_invoiced,
        ),
        rows=rows,
    )


def _invoice_record(invoice: RemediationInvoice | None) -> RemediationInvoiceRecord | None:
    if invoice is None:
        return None
    return RemediationInvoiceRecord(
        paypal_invoice_id=invoice.paypal_invoice_id,
        paypal_invoice_number=invoice.paypal_invoice_number,
        status=invoice.status,
        total_amount=invoice.total_amount,
        currency=invoice.currency,
        recipient_email=invoice.recipient_email,
        payer_view_url=invoice.invoice_view_url or "",
        qr_code_base64=invoice.qr_code_base64 or "",
        sent_at=invoice.sent_at if invoice.sent_at is not None else utcnow(),
        last_reminded_at=None,
    )


def _leash_decision(discrepancy: Discrepancy | None) -> LeashDecision | None:
    """Derive the guard verdict from the Detective-side state (A.5 ↔ Interface 2)."""
    if discrepancy is None:
        return None
    status = discrepancy.status
    if status in ("PENDING_APPROVAL", "BLOCKED"):
        return {
            "PENDING_APPROVAL": LeashDecision.ASK,
            "BLOCKED": LeashDecision.DENY,
        }[status]
    if status in ("REMEDIATED", "RECOVERED", "SEND_FAILED"):
        return LeashDecision.ALLOW
    return None


def _title(db: Session, contract_id: str) -> str:
    contract = db.get(Contract, contract_id)
    return contract.title if contract else ""


def _client(db: Session, contract_id: str) -> str:
    contract = db.get(Contract, contract_id)
    return contract.client_name if contract else ""


def _email(db: Session, contract_id: str) -> str:
    contract = db.get(Contract, contract_id)
    return contract.client_paypal_email if contract else ""
