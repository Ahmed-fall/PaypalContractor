"""Discrepancy remediation endpoint (WP-1.4): ``POST /discrepancies/{id}/remediate``.

Owner-approved money path: the guard already said Allow when the pipeline
auto-sent, else the owner's ``confirm_send: true`` is the human-in-the-loop.
A ``Blocked`` discrepancy stays blocked (409 LEASH_DENIED). Repeat calls return
409 ALREADY_REMEDIATED; a SEND_FAILED discrepancy resumes from the saved draft.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api.deps import limit_writes, require_demo_key
from ..api.errors import ApiError
from ..db.database import get_db
from ..db.models import Discrepancy
from ..remediation import remediate
from ..schemas.contracts import (
    DiscrepancyStatus,
    RemediationRequest,
    RemediationResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discrepancies", tags=["discrepancies"])


@router.post(
    "/{discrepancy_id}/remediate",
    status_code=200,
    dependencies=[Depends(require_demo_key), Depends(limit_writes)],
)
async def remediate_discrepancy(
    discrepancy_id: str,
    request: RemediationRequest,
    db: Session = Depends(get_db),
) -> RemediationResponse:
    if db.get(Discrepancy, discrepancy_id) is None:
        raise ApiError(404, "NOT_FOUND", "unknown discrepancy")

    invoice = await remediate(
        db,
        discrepancy_id,
        confirm_send=request.confirm_send,
        note_override=request.custom_note_override,
        idempotency_key=request.idempotency_key,
        actor="owner",
    )
    row = db.get(Discrepancy, discrepancy_id)
    assert row is not None
    return RemediationResponse(
        discrepancy_id=discrepancy_id,
        discrepancy_status=DiscrepancyStatus(row.status),
        remediation_invoice=invoice,
    )
