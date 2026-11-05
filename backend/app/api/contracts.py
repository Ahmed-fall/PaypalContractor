"""Contract endpoints (WP-1.4, Interface 4): extract + activate.

``POST /contracts/extract`` — multipart upload (pdf/md/txt ≤ 5 MB) → AI
compilation → persisted DRAFT contract with ``PersistedMilestoneSpec`` ids.
``POST /contracts/{contract_id}/activate`` — owner confirmation; milestone ids
assigned at extract time are preserved exactly (Rev 2.3 erratum §12.4).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy import select

from sqlalchemy.orm import Session

from ..ai import ContractParseError, extract_contract_rules
from ..api.deps import limit_extractions, require_demo_key
from ..api.errors import ApiError
from ..db.database import get_db, new_id, utcnow
from ..db.models import Contract, Milestone
from ..schemas.contracts import (
    ContractActivateRequest,
    ContractActivateResponse,
    ContractExtractResponse,
    ContractStatus,
    PersistedContractExtraction,
    PersistedMilestoneSpec,
)

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_SUFFIXES = (".pdf", ".md", ".txt")

router = APIRouter(prefix="/contracts", tags=["contracts"])


def _decode_raw(filename: str, data: bytes) -> str:
    """Best-effort stored raw text; pdfs keep their parsed clause quote instead."""
    if filename.lower().endswith(ALLOWED_SUFFIXES[1:]):
        return data.decode("utf-8", errors="ignore")
    return ""


@router.post(
    "/extract", status_code=201, dependencies=[Depends(require_demo_key), Depends(limit_extractions)]
)
async def extract_contract(
    file: Annotated[UploadFile, File(description="Signed contract: PDF, Markdown or plain text")],
    db: Session = Depends(get_db),
) -> ContractExtractResponse:
    data = await file.read()
    filename = file.filename or "contract"
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise ApiError(
            422, "UNSUPPORTED_FILE_TYPE", "supported formats: pdf, md, txt"
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "FILE_TOO_LARGE", "contract file must be 5 MB or less")
    if not data:
        raise ApiError(422, "EMPTY_FILE", "the uploaded contract is empty")

    try:
        extraction = await extract_contract_rules(data, filename)
    except ContractParseError as exc:
        raise ApiError(422, "EXTRACTION_FAILED", str(exc)[:200]) from exc

    contract_id = new_id("cnt")
    contract = Contract(
        id=contract_id,
        title=extraction.contract_title,
        client_name=extraction.client_name,
        client_paypal_email=str(extraction.client_paypal_email),
        currency=extraction.currency,
        raw_text=_decode_raw(filename, data) or extraction.penalty_clause_quote,
        late_fee_percentage=extraction.late_fee_percentage,
        grace_period_days=extraction.grace_period_days,
        allow_partial_payments=extraction.allow_partial_payments,
        penalty_clause_quote=extraction.penalty_clause_quote,
        extraction_confidence=Decimal(str(extraction.extraction_confidence)).quantize(Decimal("0.01")),
        client_tags=[],
        status=ContractStatus.DRAFT.value,
    )
    db.add(contract)

    persisted: list[PersistedMilestoneSpec] = []
    for spec in extraction.milestones:
        milestone_id = new_id("ms")
        db.add(
            Milestone(
                id=milestone_id,
                contract_id=contract_id,
                code=spec.milestone_code,
                title=spec.title,
                expected_amount=spec.expected_amount,
                due_date=spec.due_date,
                source_clause_quote=spec.source_clause_quote,
            )
        )
        persisted.append(
            PersistedMilestoneSpec(
                milestone_id=milestone_id,
                milestone_code=spec.milestone_code,
                title=spec.title,
                expected_amount=spec.expected_amount,
                due_date=spec.due_date,
                source_clause_quote=spec.source_clause_quote,
            )
        )
    db.commit()
    logger.info(
        "contract.extracted",
        extra={
            "contract": contract_id,
            "filename": filename,
            "milestones": len(persisted),
        },
    )

    return ContractExtractResponse(
        contract_id=contract_id,
        status=ContractStatus.DRAFT,
        source_filename=filename,
        extraction=PersistedContractExtraction(
            contract_title=contract.title,
            client_name=contract.client_name,
            client_paypal_email=contract.client_paypal_email,
            currency=contract.currency,
            late_fee_percentage=contract.late_fee_percentage,
            grace_period_days=contract.grace_period_days,
            allow_partial_payments=contract.allow_partial_payments,
            penalty_clause_quote=contract.penalty_clause_quote,
            milestones=persisted,
            extraction_confidence=float(contract.extraction_confidence),
        ),
    )


@router.post(
    "/{contract_id}/activate", status_code=200, dependencies=[Depends(require_demo_key)]
)
async def activate_contract(
    contract_id: str,
    request: ContractActivateRequest,
    db: Session = Depends(get_db),
) -> ContractActivateResponse:
    contract = db.get(Contract, contract_id)
    if contract is None:
        raise ApiError(404, "NOT_FOUND", "unknown contract")

    contract.title = request.contract_title
    contract.client_name = request.client_name
    contract.client_paypal_email = str(request.client_paypal_email)
    contract.currency = request.currency
    contract.late_fee_percentage = request.late_fee_percentage
    contract.grace_period_days = request.grace_period_days
    contract.allow_partial_payments = request.allow_partial_payments
    contract.penalty_clause_quote = request.penalty_clause_quote
    contract.status = ContractStatus.ACTIVE.value
    contract.activated_at = utcnow()

    for existing in db.scalars(
        select(Milestone).where(Milestone.contract_id == contract_id)
    ):
        db.delete(existing)
    for spec in request.milestones:
        db.add(
            Milestone(
                id=spec.milestone_id,
                contract_id=contract_id,
                code=spec.milestone_code,
                title=spec.title,
                expected_amount=spec.expected_amount,
                due_date=spec.due_date,
                source_clause_quote=spec.source_clause_quote,
            )
        )
    db.commit()
    logger.info(
        "contract.activated",
        extra={"contract": contract_id, "milestones": len(request.milestones)},
    )
    return ContractActivateResponse(
        contract_id=contract_id,
        status=ContractStatus.ACTIVE,
        milestones=request.milestones,
    )
