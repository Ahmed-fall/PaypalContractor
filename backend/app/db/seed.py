"""Demo seed: the Acme contract and MS-01, in the shape of A.2 fixtures.

The seed matches ``shared/fixtures/json/acme_extract.json`` so the UI shows the
same numbers whether the backend is mocked or live. Roster, timesheets and any
Leash fixtures belong to P5's ``shared/fixtures/leash/``, not to this module.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import engine, utcnow
from .models import Contract, Milestone

logger = logging.getLogger(__name__)

_CONTRACT_ID = "cnt_seed_acme"
_MILESTONE_ID = "ms_seed_01"
_CLAUSE = (
    "Section 3.1: Client shall pay $1,000.00 USD upon Phase 1 delivery"
    " on or before October 15, 2026."
)
_PENALTY_CLAUSE = (
    "Section 4.2: Any payment received below the milestone amount or more than"
    " 3 days past the due date incurs a mandatory 5.00% administrative late fee"
    " on the full milestone balance."
)


def _seed_terms() -> dict[str, object]:
    return {
        "contract_id": _CONTRACT_ID,
        "milestone_id": _MILESTONE_ID,
        "contract_title": "Master Software Development Agreement - Acme Corp",
        "client_name": "Acme Corp",
        "client_paypal_email": "sb-buyer@personal.example.com",
        "currency": "USD",
        "late_fee_percentage": Decimal("5.00"),
        "grace_period_days": 3,
        "allow_partial_payments": False,
        "penalty_clause_quote": _PENALTY_CLAUSE,
        "milestone_code": "MS-01",
        "milestone_title": "Phase 1: Backend Architecture Delivery",
        "expected_amount": Decimal("1000.00"),
        "due_date": dt.date(2026, 10, 15),
        "source_clause_quote": _CLAUSE,
    }


def active_contract_exists(db: Session) -> bool:
    return db.scalar(select(Contract.id).limit(1)) is not None


def seed_terms() -> dict[str, object]:
    """Public: scripts and reset flows read the canonical demo terms from here."""
    return _seed_terms()


def seed_demo(*, force: bool = False) -> None:
    """Seed Acme + MS-01 once, or always on ``/demo/reset`` (``force=True``)."""
    with Session(engine) as db, db.begin():
        if not force and active_contract_exists(db):
            return
        insert_contract(db)


def insert_contract(db: Session) -> Contract:
    contract = Contract(
        id=_CONTRACT_ID,
        title="Master Software Development Agreement - Acme Corp",
        client_name="Acme Corp",
        client_paypal_email="sb-buyer@personal.example.com",
        currency="USD",
        raw_text="Seeded demo contract: Acme Standard (see shared/fixtures/contracts).",
        late_fee_percentage=Decimal("5.00"),
        grace_period_days=3,
        allow_partial_payments=False,
        penalty_clause_quote=_PENALTY_CLAUSE,
        extraction_confidence=Decimal("0.98"),
        client_tags=[],
        status="ACTIVE",
        activated_at=utcnow(),
    )
    milestone = Milestone(
        id=_MILESTONE_ID,
        contract_id=contract.id,
        code="MS-01",
        title="Phase 1: Backend Architecture Delivery",
        expected_amount=Decimal("1000.00"),
        due_date=dt.date(2026, 10, 15),
        source_clause_quote=_CLAUSE,
        status="AWAITING_PAYMENT",
    )
    db.add_all([contract, milestone])
    logger.info("seed.demo_contract_ready", extra={"contract": contract.id})
    return contract


__all__ = ["active_contract_exists", "insert_contract", "seed_demo", "seed_terms"]
