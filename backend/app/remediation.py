"""Remediation orchestration: invoicing persistence + safety rules (WP-1.3).

Shared by hook H1 (auto-send when the guard allows) and the owner route
``POST /discrepancies/{id}/remediate`` so both follow one code path.

Persistence rules (PIT-11, R3):
- the ``remediation_invoices`` row is saved as ``DRAFT`` the moment PayPal
  accepts the draft (inside the executor's ``create_invoice``);
- a retry from ``SEND_FAILED`` resumes at ``/send`` only when amount and
  recipient are unchanged; otherwise the draft is deleted, ``decide()`` re-runs
  and attempt + 1 starts with its own invoice number and request id;
- the invoice number is ``FD-<last 10 of the discrepancy ULID>-R<attempt>``;
  attempt 1 keeps every AI feature (Gemini plans the workflow via P2's engine),
  retries are deterministic (no second LLM round-trip after a failure).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import (
    RemediationStepFailed,
    build_create_invoice_params,
    remediation_request_id,
    run_remediation,
)
from .ai.paypal_tools import GenerateInvoiceQrCodeParameters, SendInvoiceParameters
from .api.errors import ApiError
from .db.database import new_id, utcnow
from .db.models import Contract, Discrepancy, Milestone, PaypalCapture, RemediationInvoice
from .leash_hooks import decide as leash_decide
from .schemas.contracts import (
    ContractActivateRequest,
    DiscrepancyEvaluationResult,
    DiscrepancyStatus,
    DiscrepancyType,
    ExtractedMilestoneSpec,
    GuardAction,
    InvoiceLineItemSpec,
    LeashDecision,
    PersistedMilestoneSpec,
    RemediationInvoiceRecord,
)
from .services.paypal.invoices import (
    InvoicingExecutor,
    delete_draft,
    fetch_payer_view,
    invoice_number,
)

logger = logging.getLogger(__name__)

FRESH_WINDOW_DAYS = 7
Sender = Literal["owner", "leash-auto"]


# --- context loading + reconstruction --------------------------------------------------------


def _context(db: Session, discrepancy: Discrepancy) -> tuple[Contract, Milestone]:
    milestone = db.get(Milestone, discrepancy.milestone_id)
    if milestone is None:
        raise ApiError(404, "NOT_FOUND", "discrepancy has no milestone")
    contract = db.get(Contract, milestone.contract_id)
    if contract is None:
        raise ApiError(404, "NOT_FOUND", "discrepancy has no contract")
    return contract, milestone


def _ai_contract_terms(contract: Contract, milestone: Milestone) -> ContractActivateRequest:
    return ContractActivateRequest(
        contract_title=contract.title,
        client_name=contract.client_name,
        client_paypal_email=contract.client_paypal_email,
        currency=contract.currency,
        late_fee_percentage=contract.late_fee_percentage,
        grace_period_days=contract.grace_period_days,
        allow_partial_payments=contract.allow_partial_payments,
        penalty_clause_quote=contract.penalty_clause_quote,
        milestones=[_persisted_spec(milestone)],
    )


def _persisted_spec(milestone: Milestone) -> PersistedMilestoneSpec:
    return PersistedMilestoneSpec(
        milestone_id=milestone.id,
        milestone_code=milestone.code,
        title=milestone.title,
        expected_amount=milestone.expected_amount,
        due_date=milestone.due_date,
        source_clause_quote=milestone.source_clause_quote,
    )


def _stored_evaluation(
    db: Session, discrepancy: Discrepancy
) -> DiscrepancyEvaluationResult:
    """Rebuild the stored evaluation: retry never runs the LLM twice."""
    from .pipeline import api_status

    capture = db.get(PaypalCapture, discrepancy.capture_id)
    captured_amount = capture.gross_amount if capture else Decimal("0.00")
    expected_amount = (captured_amount + discrepancy.shortfall_amount).quantize(Decimal("0.01"))
    items = [
        InvoiceLineItemSpec.model_validate(item)
        for item in (discrepancy.line_items_json or [])
    ]
    return DiscrepancyEvaluationResult(
        discrepancy_type=DiscrepancyType(discrepancy.discrepancy_type),
        discrepancy_status=DiscrepancyStatus(api_status(discrepancy.status)),
        is_compliant=False,
        expected_amount=expected_amount,
        captured_amount=captured_amount,
        captured_currency=capture.currency if capture else None,
        shortfall_amount=discrepancy.shortfall_amount,
        penalty_amount=discrepancy.penalty_amount,
        total_remediation_amount=discrepancy.total_remediation_amount,
        violated_clause_quote=discrepancy.violated_clause_quote or "",
        ai_audit_summary=discrepancy.ai_audit_summary,
        paypal_invoice_detail_note=discrepancy.draft_invoice_note or "",
        remediation_line_items=items,
    )


# --- guard facts (P5 vocabulary; rules G-01..G-05) --------------------------------------------


def _guard_facts(
    db: Session, discrepancy: Discrepancy, contract: Contract
) -> dict[str, Any]:
    items = discrepancy.line_items_json or []
    summed = sum(
        (Decimal(str(item["unit_amount_value"])) for item in items),
        Decimal("0.00"),
    )
    fresh = db.scalar(
        select(RemediationInvoice.id)
        .where(RemediationInvoice.status == "SENT")
        .where(RemediationInvoice.recipient_email == contract.client_paypal_email)
        .where(RemediationInvoice.sent_at > utcnow() - timedelta(days=FRESH_WINDOW_DAYS))
        .limit(1)
    )
    return {
        "amount": f"{discrepancy.total_remediation_amount:.2f}",
        "extraction_confidence": float(contract.extraction_confidence),
        "has_clause_quote": bool(discrepancy.violated_clause_quote),
        "client_tags": list(contract.client_tags or []),
        "days_since_last_client_invoice": FRESH_WINDOW_DAYS - 1
        if fresh
        else FRESH_WINDOW_DAYS + 1,
        "invoiced_same_client_within_days": FRESH_WINDOW_DAYS if fresh else 0,
        "line_items_match_decimal_total": summed == discrepancy.total_remediation_amount,
        "line_items_sum_mismatch": summed != discrepancy.total_remediation_amount,
    }


# --- invoice rows + executor ------------------------------------------------------------------


def _record(invoice: RemediationInvoice) -> RemediationInvoiceRecord:
    return RemediationInvoiceRecord(
        paypal_invoice_id=invoice.paypal_invoice_id,
        paypal_invoice_number=invoice.paypal_invoice_number,
        status=invoice.status,
        total_amount=invoice.total_amount,
        currency=invoice.currency,
        recipient_email=invoice.recipient_email,
        payer_view_url=invoice.invoice_view_url or "",
        qr_code_base64=invoice.qr_code_base64 or "",
        sent_at=invoice.sent_at or utcnow(),
    )


def latest_invoice(db: Session, discrepancy_id: str) -> RemediationInvoice | None:
    return db.scalar(
        select(RemediationInvoice)
        .where(RemediationInvoice.discrepancy_id == discrepancy_id)
        .order_by(RemediationInvoice.attempt.desc())
        .limit(1)
    )


def row_by_paypal_id(db: Session, invoice_id: str) -> RemediationInvoice | None:
    return db.scalar(
        select(RemediationInvoice).where(
            RemediationInvoice.paypal_invoice_id == invoice_id
        )
    )


def _persist_draft(
    db: Session,
    *,
    discrepancy: Discrepancy,
    contract: Contract,
    attempt: int,
    invoice_id: str,
    request_id: str,
) -> RemediationInvoice:
    row = RemediationInvoice(
        id=new_id("inv"),
        discrepancy_id=discrepancy.id,
        attempt=attempt,
        paypal_invoice_id=invoice_id.strip(),
        paypal_invoice_number=invoice_number(discrepancy.id, attempt),
        recipient_email=contract.client_paypal_email,
        total_amount=discrepancy.total_remediation_amount,
        currency=contract.currency,
        status="DRAFT",
        paypal_request_id=request_id,
    )
    db.add(row)
    db.commit()
    logger.info(
        "invoice.draft_persisted",
        extra={
            "discrepancy": discrepancy.id,
            "attempt": attempt,
            "paypal_invoice": invoice_id,
        },
    )
    return row


def _mark_sent(
    db: Session,
    row: RemediationInvoice,
    *,
    actor: Sender,
    payer_view: str | None,
    qr_code: str | None,
) -> None:
    row.status = "SENT"
    row.sent_by = actor
    row.sent_at = utcnow()
    row.invoice_view_url = payer_view or row.invoice_view_url
    row.qr_code_base64 = qr_code or row.qr_code_base64
    db.add(row)
    db.commit()
    logger.info(
        "invoice.sent",
        extra={"discrepancy": row.discrepancy_id, "attempt": row.attempt, "sent_by": actor},
    )


class PersistingExecutor(InvoicingExecutor):
    """P2's executor protocol extended with PIT-11 persistence at draft time."""

    def __init__(
        self,
        db: Session,
        *,
        discrepancy: Discrepancy,
        contract: Contract,
        attempt: int,
    ) -> None:
        super().__init__(discrepancy_id=discrepancy.id, attempt=attempt)
        self._db = db
        self._discrepancy = discrepancy
        self._contract = contract
        self._attempt = attempt

    async def create_invoice(self, params: Any, request_id: str) -> str:
        invoice_id = await super().create_invoice(params, request_id)
        _persist_draft(
            self._db,
            discrepancy=self._discrepancy,
            contract=self._contract,
            attempt=self._attempt,
            invoice_id=invoice_id,
            request_id=request_id,
        )
        return invoice_id


def _per_attempt_request_id(discrepancy_id: str, attempt: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{discrepancy_id}:remediate:R{attempt}"))


# --- the remediation flow ----------------------------------------------------------------------


async def remediate(
    db: Session,
    discrepancy_id: str,
    *,
    confirm_send: bool,
    note_override: str | None = None,
    idempotency_key: str | None = None,
    actor: Sender = "owner",
) -> RemediationInvoiceRecord:
    """Create + persist the draft (PIT-11), send it, generate the QR code.

    Hook H1 calls this with ``actor='leash-auto'`` after ``decide()`` returned
    Allow; the owner route calls it with ``confirm_send=True``. Raises ``ApiError``
    codes the frontend can act on (409 ALREADY_REMEDIATED / LEASH_DENIED,
    502 PAYPAL_ERROR).
    """
    del idempotency_key  # semantics are carried by the DB unique keys
    discrepancy = db.get(Discrepancy, discrepancy_id)
    if discrepancy is None:
        raise ApiError(404, "NOT_FOUND", "unknown discrepancy")
    contract, milestone = _context(db, discrepancy)

    if _has_sent_invoice(db, discrepancy.id):
        raise ApiError(409, "ALREADY_REMEDIATED", "this discrepancy is already remediated")
    if discrepancy.status == DiscrepancyStatus.BLOCKED.value:
        raise ApiError(409, "LEASH_DENIED", "Leash blocked this remediation")
    if actor == "owner" and not confirm_send:
        raise ApiError(
            409,
            "REMEDIATION_NOT_CONFIRMED",
            "set confirm_send=true to send the invoice",
        )

    latest = latest_invoice(db, discrepancy.id)
    attempt = 1
    evaluation = _stored_evaluation(db, discrepancy)
    if latest is not None and latest.status == "DRAFT":
        if (
            latest.total_amount == discrepancy.total_remediation_amount
            and latest.recipient_email == contract.client_paypal_email
        ):
            return await _resume_send(db, discrepancy, latest, actor)
        # Stale amount or recipient: delete, re-check with the guard, retry (R3).
        await delete_draft(latest.paypal_invoice_id)
        latest.status = "CANCELLED"
        db.add(latest)
        rechecked = await leash_decide(
            GuardAction.SEND_REMEDIATION_INVOICE,
            _guard_facts(db, discrepancy, contract),
            discrepancy.id,
        )
        if rechecked is not None and rechecked.decision != LeashDecision.ALLOW:
            discrepancy.status = _status_for(rechecked.decision)
            discrepancy.leash_decision_id = rechecked.decision_id
            db.add(discrepancy)
            db.commit()
            logger.info(
                "remediation.rechecked",
                extra={
                    "discrepancy": discrepancy.id,
                    "decision": rechecked.decision.value,
                },
            )
            raise ApiError(
                409,
                "LEASH_ASK_REQUIRED"
                if rechecked.decision == LeashDecision.ASK
                else "LEASH_DENIED",
                str(rechecked.reason) or "the guard re-checked this remediation",
            )
        attempt = latest.attempt + 1

    try:
        if attempt == 1:
            # Fresh remediation: P2's engine plans and drives the workflow.
            run = await run_remediation(
                _ai_contract_terms(contract, milestone),
                _extracted_spec(milestone),
                evaluation,
                PersistingExecutor(
                    db, discrepancy=discrepancy, contract=contract, attempt=1
                ),
                discrepancy_id=discrepancy.id,
                confirm_send=True,
                note_override=note_override,
            )
            row = _persisted_row_or_raise(
                db, run.invoice_id, discrepancy, contract, remediation_request_id(discrepancy.id)
            )
            payer_view = await fetch_payer_view(run.invoice_id)
            _mark_sent(db, row, actor=actor, payer_view=payer_view, qr_code=run.qr_code_base64)
        else:
            # Deterministic retry: new draft + request id per attempt (R3).
            request_id = _per_attempt_request_id(discrepancy.id, attempt)
            params = build_create_invoice_params(
                _ai_contract_terms(contract, milestone),
                _extracted_spec(milestone),
                evaluation,
                note_override=note_override,
            )
            executor = PersistingExecutor(
                db, discrepancy=discrepancy, contract=contract, attempt=attempt
            )
            invoice_id = await executor.create_invoice(params, request_id)
            await executor.send_invoice(SendInvoiceParameters(invoice_id=invoice_id))
            row = _persisted_row_or_raise(
                db, invoice_id, discrepancy, contract, request_id
            )
            qr = await executor.generate_invoice_qr_code(
                GenerateInvoiceQrCodeParameters(invoice_id=invoice_id)
            )
            payer_view = await fetch_payer_view(invoice_id)
            _mark_sent(db, row, actor=actor, payer_view=payer_view, qr_code=qr)
    except RemediationStepFailed as exc:
        discrepancy.status = "SEND_FAILED"  # DB lifecycle state (A.5); API enum keeps 4 values
        discrepancy.last_error = str(exc)[:200]
        db.add(discrepancy)
        db.commit()
        logger.warning(
            "invoice.send_failed",
            extra={"discrepancy": discrepancy.id, "step": exc.step},
        )
        raise ApiError(
            502,
            "PAYPAL_ERROR",
            "the invoice could not be sent; the retained draft resumes on retry",
            {"paypal_step": exc.step, "paypal_invoice": exc.invoice_id or ""},
        ) from exc

    discrepancy.status = DiscrepancyStatus.REMEDIATED.value
    db.add(discrepancy)
    db.commit()
    return _record(row)


async def _resume_send(
    db: Session,
    discrepancy: Discrepancy,
    draft: RemediationInvoice,
    actor: Sender,
) -> RemediationInvoiceRecord:
    """Same amount and recipient: continue at ``/send`` without re-creating (R3)."""
    executor = InvoicingExecutor(
        discrepancy_id=discrepancy.id, attempt=draft.attempt
    )
    try:
        await executor.send_invoice(
            SendInvoiceParameters(invoice_id=draft.paypal_invoice_id)
        )
        qr = await executor.generate_invoice_qr_code(
            GenerateInvoiceQrCodeParameters(invoice_id=draft.paypal_invoice_id)
        )
    except RemediationStepFailed as exc:  # pragma: no cover - express lane of step ladder
        raise ApiError(502, "PAYPAL_ERROR", "the invoice could not be sent") from exc
    payer_view = await fetch_payer_view(draft.paypal_invoice_id)
    _mark_sent(db, draft, actor=actor, payer_view=payer_view, qr_code=qr)

    discrepancy.status = DiscrepancyStatus.REMEDIATED.value
    db.add(discrepancy)
    db.commit()
    return _record(draft)


def _extracted_spec(milestone: Milestone) -> ExtractedMilestoneSpec:
    return ExtractedMilestoneSpec(
        milestone_code=milestone.code,
        title=milestone.title,
        expected_amount=milestone.expected_amount,
        due_date=milestone.due_date,
        source_clause_quote=milestone.source_clause_quote,
    )


def _persisted_row_or_raise(
    db: Session,
    invoice_id: str,
    discrepancy: Discrepancy,
    contract: Contract,
    request_id: str,
) -> RemediationInvoice:
    row = row_by_paypal_id(db, invoice_id)
    if row is None:
        # Draft accepted but persistence never saw it: durable fallback that
        # keeps PIT-11 from ever losing the invoice row.
        row = _persist_draft(
            db,
            discrepancy=discrepancy,
            contract=contract,
            attempt=latest_attempt(db, discrepancy.id) + 1,
            invoice_id=invoice_id,
            request_id=request_id,
        )
    return row


def latest_attempt(db: Session, discrepancy_id: str) -> int:
    latest = latest_invoice(db, discrepancy_id)
    return latest.attempt if latest else 0


def _has_sent_invoice(db: Session, discrepancy_id: str) -> bool:
    return (
        db.scalar(
            select(RemediationInvoice.id)
            .where(RemediationInvoice.discrepancy_id == discrepancy_id)
            .where(RemediationInvoice.status == "SENT")
            .limit(1)
        )
        is not None
    )


def _status_for(decision: LeashDecision) -> str:
    return {
        LeashDecision.ASK: DiscrepancyStatus.PENDING_APPROVAL.value,
        LeashDecision.DENY: DiscrepancyStatus.BLOCKED.value,
    }.get(decision, DiscrepancyStatus.NONE.value)


# Public surface for the pipeline layer (same-package helpers).
record = _record
guard_facts = _guard_facts
ai_contract_terms = _ai_contract_terms
extracted_spec = _extracted_spec
