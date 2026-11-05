"""PayPal webhook receiver (WP-1.4): verify → dedupe → route.

Open endpoint (no demo key) but never bypassed: every delivery must pass
``verify-webhook-signature`` (SUCCESS) before the database is touched.
``webhook_events`` is keyed by the PayPal event id; repeated deliveries
short-circuit with 200 (§13 webhook hardening). Routes:
- ``PAYMENT.CAPTURE.COMPLETED`` → capture-first claim (PIT-03): the winner runs
  the audit pipeline, the loser only flags ``webhook_verified``; a stale claim
  is re-claimed (R2)
- ``INVOICING.INVOICE.PAID`` → invoice PAID + discrepancy RECOVERED (A.5)
- ``PAYMENT.PAYOUTS*`` → dispatched to P5's ``payouts_sync`` (guarded import)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api.errors import ApiError
from ..config import get_settings
from ..db.database import conditional_update, conflict_insert, get_db, utcnow
from ..db.models import PaypalCapture, RemediationInvoice, WebhookEvent, Discrepancy
from ..leash_hooks import PAYOUTS_EVENT_PREFIX, dispatch_payouts_event
from ..pipeline import (
    claim_capture,
    flag_webhook_verified,
    reclaim_capture,
    run_capture_pipeline,
)
from ..services.paypal.webhooks import (
    WebhookVerificationError,
    parse_event,
    required_headers_present,
    verify_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/paypal", status_code=200)
async def paypal_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    settings = get_settings()
    if not settings.paypal_webhook_id:
        raise ApiError(
            503, "PAYPAL_WEBHOOK_NOT_CONFIGURED", "no PAYPAL_WEBHOOK_ID in the environment"
        )

    raw = await request.body()
    headers = request.headers
    if not required_headers_present(headers):
        raise ApiError(
            400, "WEBHOOK_HEADERS_MISSING", "missing PayPal transmission headers"
        )
    event = parse_event(raw)
    event_id = str(event.get("id", ""))
    if not event_id:
        raise ApiError(400, "WEBHOOK_EVENT_MALFORMED", "event id missing")
    try:
        await verify_signature(headers, event, webhook_id=settings.paypal_webhook_id)
    except WebhookVerificationError as exc:
        raise ApiError(400, "WEBHOOK_SIGNATURE_INVALID", str(exc)[:200]) from exc

    resource = event.get("resource") or {}
    won = conflict_insert(
        db,
        WebhookEvent,
        {
            "id": event_id,
            "event_type": str(event.get("event_type", "")),
            "resource_id": str(resource.get("id", "")) or None,
            "verification_status": "SUCCESS",
            "received_at": utcnow(),
            "payload": event,
        },
        "id",
    )
    db.commit()
    if not won:
        logger.info("webhook.deduped", extra={"event": event_id})
        return {"received": True}

    return await _route(db, event_id, str(event.get("event_type", "")), resource)


async def _route(
    db: Session, event_id: str, event_type: str, resource: dict[str, Any]
) -> dict[str, bool]:
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        return await _capture_completed(db, resource)
    if event_type == "INVOICING.INVOICE.PAID":
        return _invoice_paid(db, resource)
    if event_type.startswith(PAYOUTS_EVENT_PREFIX):
        await dispatch_payouts_event(
            {"id": event_id, "event_type": event_type, "resource": resource}
        )
        return {"received": True}
    logger.info("webhook.ignored", extra={"type": event_type})
    return {"received": True}


def _captured_at(resource: dict[str, Any]) -> datetime:
    stamp = resource.get("update_time") or resource.get("create_time") or ""
    if stamp:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return parsed.astimezone(UTC)
    return utcnow()


def _this_capture(db: Session, paypal_capture_id: str) -> PaypalCapture | None:
    anchor = db.scalar(
        select(PaypalCapture.id).where(
            PaypalCapture.paypal_capture_id == paypal_capture_id
        )
    )
    return db.get(PaypalCapture, anchor) if anchor else None


async def _capture_completed(
    db: Session, resource: dict[str, Any]
) -> dict[str, bool]:
    amount = resource.get("amount") or {}
    supplementary = resource.get("supplementary_data") or {}
    related = supplementary.get("related_ids") or {}
    paypal_capture_id = str(resource.get("id", ""))

    anchor = claim_capture(
        db,
        paypal_capture_id=paypal_capture_id,
        paypal_order_id=str(related.get("order_id", "")),
        milestone_id=str(resource.get("custom_id", "")),
        gross_amount=amount.get("value", "0.00"),
        currency=str(amount.get("currency_code", "USD")),
        payer_email=(resource.get("payer") or {}).get("email_address"),
        captured_at=_captured_at(resource),
        raw_payload=resource,
        source="WEBHOOK",
    )
    if anchor is None:
        # Someone else owns the claim: converge, never re-run (PIT-03).
        existing = _this_capture(db, paypal_capture_id)
        if existing is not None and existing.processed_at is None:
            if reclaim_capture(db, existing):
                logger.warning(
                    "webhook.reclaimed_pipeline",
                    extra={"capture": existing.id},
                )
                await run_capture_pipeline(db, existing.id)
            else:
                await flag_webhook_verified(db, existing.id)
        return {"received": True}

    try:
        await run_capture_pipeline(db, anchor)
    except ApiError as exc:
        logged = {"capture": anchor, "error": exc.message}
        logger.warning("webhook.pipeline_incomplete", extra=logged)
    return {"received": True}


def _invoice_paid(db: Session, resource: dict[str, Any]) -> dict[str, bool]:
    paypal_invoice_id = str(resource.get("id", ""))
    invoice = db.scalar(
        select(RemediationInvoice).where(
            RemediationInvoice.paypal_invoice_id == paypal_invoice_id
        )
    )
    if invoice is None:
        logger.info("webhook.unknown_invoice", extra={"invoice": paypal_invoice_id})
        return {"received": True}

    invoice.status = "PAID"
    db.add(invoice)
    db.execute(
        conditional_update(Discrepancy, {"status": "RECOVERED"}).where(
            Discrepancy.id == invoice.discrepancy_id
        )
    )
    db.commit()
    logger.info("invoice.recovered", extra={"invoice": paypal_invoice_id})
    return {"received": True}
