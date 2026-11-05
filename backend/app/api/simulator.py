"""Client Payment Simulator (WP-1.4): live PayPal Orders → capture → pipeline.

``POST /simulator/pay`` settles money in through the real sandbox: the order is
created (card payment source), captured, and the authoritative capture response
claims the ``paypal_captures`` anchor (PIT-03) before the audit pipeline runs.
A repeat with the same idempotency key produces the same capture id; the caller
then gets ``409 CAPTURE_ALREADY_PROCESSED`` instead of a second audit run.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, time

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api.deps import limit_writes, require_demo_key
from ..api.errors import ApiError
from ..db.database import get_db
from ..db.models import Contract, Milestone
from ..pipeline import claim_capture, run_capture_pipeline
from ..schemas.contracts import SimulatorPaymentRequest, SimulatorPaymentResponse
from ..services.paypal import orders

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/simulator", tags=["simulator"])


@router.post("/pay", status_code=200, dependencies=[Depends(require_demo_key), Depends(limit_writes)])
async def pay(
    request: SimulatorPaymentRequest,
    db: Session = Depends(get_db),
) -> SimulatorPaymentResponse:
    milestone = db.get(Milestone, request.milestone_id)
    if milestone is None:
        raise ApiError(404, "NOT_FOUND", "unknown milestone")
    contract = db.get(Contract, milestone.contract_id)
    if contract is None or contract.status != "ACTIVE":
        raise ApiError(
            409, "CONTRACT_NOT_ACTIVE", "activate the contract before sending payments"
        )

    request_id = f"{milestone.id}:{request.idempotency_key or uuid.uuid4()}"
    order = await orders.create_order(
        amount=request.simulated_amount,
        currency=request.currency,
        milestone_id=milestone.id,
        payer_email=str(request.payer_email),
        request_id=request_id,
    )
    capture = await orders.capture_order(
        order_id=str(order["id"]), request_id=f"{request_id}:capture"
    )
    fields = orders.capture_fields(capture)
    # The simulated clock is the payment date the owner chose; the authoritative
    # seconds come from the sandbox response body only for real webhooks.
    captured_at = datetime.combine(request.simulated_payment_date, time(12, 0), tzinfo=UTC)

    anchor = claim_capture(
        db,
        paypal_capture_id=fields[0],
        paypal_order_id=fields[1],
        milestone_id=milestone.id,
        gross_amount=fields[2],
        currency=fields[3],
        payer_email=fields[4],
        captured_at=captured_at,
        raw_payload=capture,
        source="SIMULATOR",
    )
    if anchor is None:
        raise ApiError(
            409,
            "CAPTURE_ALREADY_PROCESSED",
            f"capture {fields[0]} was already audited (idempotent replay)",
            {"paypal_capture_id": fields[0]},
        )

    try:
        return await run_capture_pipeline(db, anchor, subject_id="simulator")
    except ApiError as exc:
        if exc.status_code == 502 and "PAYPAL_ERROR" not in (exc.code,):
            raise
        logger.warning(
            "simulator.pipeline_incomplete",
            extra={"milestone": milestone.id, "error": exc.message[:200]},
        )
        raise
