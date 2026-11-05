"""Capture-first audit pipeline (WP-1.2; plan §7.2 steps 5–13, PIT-03, R2).

Order is fixed by PIT-03: the ``paypal_captures`` anchor row is committed in its
own transaction with ``ON CONFLICT (paypal_capture_id) DO NOTHING`` before any
evaluation, hook or remediation runs. The claim winner runs the pipeline; the
loser only flags ``webhook_verified``. A claim older than 30 s with no
``processed_at`` is re-claimed atomically (crash recovery, R2).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import evaluate_payment_compliance
from .api.errors import ApiError
from .db.database import conflict_insert, conditional_update, new_id, utcnow
from .db.models import Contract, Discrepancy, Milestone, PaypalCapture
from .leash_hooks import decide as leash_decide
from .leash_hooks import leash_enabled, on_milestone_payment
from .remediation import (
    ai_contract_terms,
    extracted_spec,
    guard_facts,
    remediate,
)
from .schemas.contracts import (
    DiscrepancyEvaluationResult,
    DiscrepancyStatus,
    DiscrepancyType,
    GuardAction,
    LeashDecision,
    RemediationInvoiceRecord,
    SimulatorPaymentResponse,
)

logger = logging.getLogger(__name__)

CLAIM_STALE_SECONDS = 30

_API_STATUS: dict[str, DiscrepancyStatus] = {
    "OPEN": DiscrepancyStatus.NONE,
    "NONE": DiscrepancyStatus.NONE,
    "PENDING_APPROVAL": DiscrepancyStatus.PENDING_APPROVAL,
    "BLOCKED": DiscrepancyStatus.BLOCKED,
    "REMEDIATED": DiscrepancyStatus.REMEDIATED,
    "SEND_FAILED": DiscrepancyStatus.NONE,
    "RECOVERED": DiscrepancyStatus.REMEDIATED,
}


def api_status(db_status: str | None) -> str:
    """API status enum (A.3) from the richer DB lifecycle (A.5)."""
    return _API_STATUS.get(db_status or "NONE", DiscrepancyStatus.NONE).value


def claim_capture(
    db: Session,
    *,
    paypal_capture_id: str,
    paypal_order_id: str,
    milestone_id: str,
    gross_amount: Decimal,
    currency: str,
    payer_email: str | None,
    captured_at: datetime,
    raw_payload: dict[str, Any],
    source: Literal["SIMULATOR", "WEBHOOK"],
) -> str | None:
    """PIT-03 winner anchor. Returns the db capture id, or None if already there."""
    anchor = new_id("cap")
    won = conflict_insert(
        db,
        PaypalCapture,
        {
            "id": anchor,
            "paypal_capture_id": paypal_capture_id,
            "paypal_order_id": paypal_order_id,
            "milestone_id": milestone_id,
            "gross_amount": gross_amount,
            "currency": currency,
            "payer_email": payer_email,
            "captured_at": captured_at,
            "source": source,
            "webhook_verified": source == "WEBHOOK",
            "claimed_at": utcnow(),
            "raw_payload_json": raw_payload,
        },
        "paypal_capture_id",
    )
    db.commit()
    if not won:
        logger.info(
            "capture.claim_lost",
            extra={"paypal_capture": paypal_capture_id, "source": source},
        )
        return None
    return anchor


def capture_row(db: Session, anchor: str) -> PaypalCapture:
    capture = db.get(PaypalCapture, anchor)
    if capture is None:
        raise ApiError(404, "NOT_FOUND", "capture anchor vanished")
    return capture


def reclaim_capture(db: Session, capture: PaypalCapture) -> bool:
    """R2: a claim older than 30 s with no ``processed_at`` is re-claimed."""
    if capture.processed_at is not None:
        return False
    claimed_at = capture.claimed_at
    if claimed_at is None:
        return False
    if (utcnow() - claimed_at).total_seconds() < CLAIM_STALE_SECONDS:
        return False
    result = db.execute(
        conditional_update(PaypalCapture, {"claimed_at": utcnow()}).where(
            PaypalCapture.id == capture.id,
            PaypalCapture.processed_at.is_(None),
        )
    )
    db.commit()
    won = int(getattr(result, "rowcount", 0) or 0) > 0
    if won:
        logger.warning("capture.reclaimed", extra={"capture": capture.id})
    return won


async def flag_webhook_verified(db: Session, anchor: str) -> None:
    """The webhook-converge path: never runs the pipeline, only verifies."""
    db.execute(
        conditional_update(
            PaypalCapture, {"webhook_verified": True}
        ).where(PaypalCapture.id == anchor)
    )
    db.commit()


async def run_capture_pipeline(
    db: Session,
    anchor: str,
    *,
    payer_note: str = "",
    subject_id: str = "simulated",
) -> SimulatorPaymentResponse:
    """Evaluate a claimed capture and run the full Detective + Leash flow."""
    capture = capture_row(db, anchor)
    milestone = db.get(Milestone, capture.milestone_id)
    if milestone is None:
        mark_processed(db, capture)
        logger.warning(
            "capture.unmatched", extra={"paypal_capture": capture.paypal_capture_id}
        )
        raise ApiError(404, "NOT_FOUND", "capture references an unknown milestone")
    contract = db.get(Contract, milestone.contract_id)
    if contract is None:
        raise ApiError(404, "NOT_FOUND", "capture references an unknown contract")

    evaluation: DiscrepancyEvaluationResult = await evaluate_payment_compliance(
        ai_contract_terms(contract, milestone),
        extracted_spec(milestone),
        captured_amount=capture.gross_amount,
        captured_currency=capture.currency,
        captured_date=date.fromisoformat(str(capture.captured_at.date())) if capture.captured_at else date.today(),
        payer_note=payer_note,
    )

    discrepancy: Discrepancy | None = None
    if evaluation.discrepancy_type != DiscrepancyType.COMPLIANT:
        discrepancy = db.scalar(
            select(Discrepancy).where(Discrepancy.capture_id == capture.id)
        )
        if discrepancy is None:
            discrepancy = _persist_discrepancy(db, milestone, capture, evaluation)

    leash_result = None
    invoice_record: RemediationInvoiceRecord | None = None
    if discrepancy is not None and leash_enabled():
        leash_result = await leash_decide(
            GuardAction.SEND_REMEDIATION_INVOICE,
            guard_facts(db, discrepancy, contract),
            subject_id,
        )
        if leash_result.decision == LeashDecision.ALLOW:
            invoice_record = await remediate(
                db,
                discrepancy.id,
                confirm_send=True,
                actor="leash-auto",
            )
        else:
            _apply_decision(db, discrepancy, leash_result.decision, leash_result.decision_id)

    payroll_run_id: str | None = None
    try:
        payroll_run_id = await on_milestone_payment(milestone.id)
    except Exception as exc:  # pragma: no cover - payroll hook may be absent
        logger.error(
            "payroll.hook_failed",
            extra={"milestone": milestone.id, "error": str(exc)},
        )

    mark_processed(db, capture)
    return SimulatorPaymentResponse(
        paypal_order_id=capture.paypal_order_id,
        paypal_capture_id=capture.paypal_capture_id,
        capture_status="COMPLETED",
        discrepancy_id=discrepancy.id if discrepancy else None,
        evaluation=evaluation,
        leash=leash_result if leash_result is not None else None,
        remediation_invoice=invoice_record,
        payroll_run_id=payroll_run_id,
    )


def _persist_discrepancy(
    db: Session,
    milestone: Milestone,
    capture: PaypalCapture,
    evaluation: DiscrepancyEvaluationResult,
) -> Discrepancy:
    row = Discrepancy(
        id=new_id("dsc"),
        milestone_id=milestone.id,
        capture_id=capture.id,
        discrepancy_type=evaluation.discrepancy_type.value,
        shortfall_amount=evaluation.shortfall_amount,
        penalty_amount=evaluation.penalty_amount,
        total_remediation_amount=evaluation.total_remediation_amount,
        violated_clause_quote=evaluation.violated_clause_quote or None,
        ai_audit_summary=evaluation.ai_audit_summary,
        draft_invoice_note=evaluation.paypal_invoice_detail_note,
        line_items_json=[
            item.model_dump(mode="json") for item in evaluation.remediation_line_items
        ],
        status="OPEN",
    )
    db.add(row)
    db.commit()
    logger.info(
        "discrepancy.opened",
        extra={
            "discrepancy": row.id,
            "type": row.discrepancy_type,
            "milestone": milestone.id,
        },
    )
    return row


def _apply_decision(
    db: Session,
    discrepancy: Discrepancy,
    decision: LeashDecision,
    decision_id: str,
) -> None:
    if decision == LeashDecision.DENY:
        discrepancy.status = DiscrepancyStatus.BLOCKED.value
    elif decision == LeashDecision.ASK:
        discrepancy.status = DiscrepancyStatus.PENDING_APPROVAL.value
    else:  # ALLOW is handled by remediate() right before this call
        return
    discrepancy.leash_decision_id = decision_id
    db.add(discrepancy)
    db.commit()


def mark_processed(db: Session, capture: PaypalCapture) -> None:
    capture.processed_at = utcnow()
    capture.claimed_at = capture.claimed_at or utcnow()
    db.add(capture)
    db.commit()
    logger.info("capture.processed", extra={"capture": capture.id})
