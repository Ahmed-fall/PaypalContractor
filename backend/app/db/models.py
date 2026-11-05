"""Detective tables (plan A.3). Leash tables live in ``app.leash.models`` (P5).

All money is ``Numeric(12, 2)``; IDs are prefixed lower-case ULID strings created
in Python. ``discrepancies.leash_decision_id`` stays a plain nullable string:
Nour's Leash migration turns it into the foreign key once ``leash_decisions``
exists, so this table deliberately does not declare that relationship.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, utcnow

_MONEY = Numeric(12, 2)
_STAMP = DateTime(timezone=True)


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (CheckConstraint("grace_period_days >= 0"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    client_name: Mapped[str] = mapped_column(String(120))
    client_paypal_email: Mapped[str] = mapped_column(String(254))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    raw_text: Mapped[str] = mapped_column(Text)
    late_fee_percentage: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0.00")
    )
    grace_period_days: Mapped[int] = mapped_column(default=0)
    allow_partial_payments: Mapped[bool] = mapped_column(default=False)
    penalty_clause_quote: Mapped[str] = mapped_column(Text)
    extraction_confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2))
    client_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    created_at: Mapped[dt.datetime] = mapped_column(_STAMP, default=utcnow)
    activated_at: Mapped[dt.datetime | None] = mapped_column(
        _STAMP, default=None, nullable=True
    )


class Milestone(Base):
    __tablename__ = "milestones"
    __table_args__ = (
        CheckConstraint("expected_amount > 0"),
        UniqueConstraint("contract_id", "code"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    contract_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("contracts.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    expected_amount: Mapped[Decimal] = mapped_column(_MONEY)
    due_date: Mapped[dt.date] = mapped_column(Date)
    source_clause_quote: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="AWAITING_PAYMENT")


class PaypalCapture(Base):
    __tablename__ = "paypal_captures"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    milestone_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("milestones.id"), default=None, nullable=True
    )
    paypal_order_id: Mapped[str] = mapped_column(String(64))
    paypal_capture_id: Mapped[str] = mapped_column(
        String(64), unique=True
    )  # idempotency anchor (PIT-03/06)
    payer_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    gross_amount: Mapped[Decimal] = mapped_column(_MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    captured_at: Mapped[dt.datetime] = mapped_column(_STAMP)
    source: Mapped[str] = mapped_column(String(16))
    webhook_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    claimed_at: Mapped[dt.datetime] = mapped_column(_STAMP, default=utcnow)
    processed_at: Mapped[dt.datetime | None] = mapped_column(
        _STAMP, default=None, nullable=True
    )
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class Discrepancy(Base):
    __tablename__ = "discrepancies"
    __table_args__ = (
        CheckConstraint(
            "total_remediation_amount = shortfall_amount + penalty_amount",
            name="ck_remediation_total",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    milestone_id: Mapped[str] = mapped_column(String(32), ForeignKey("milestones.id"))
    capture_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("paypal_captures.id"), unique=True
    )
    discrepancy_type: Mapped[str] = mapped_column(String(24))
    shortfall_amount: Mapped[Decimal] = mapped_column(_MONEY)
    penalty_amount: Mapped[Decimal] = mapped_column(_MONEY)
    total_remediation_amount: Mapped[Decimal] = mapped_column(_MONEY)
    violated_clause_quote: Mapped[str | None] = mapped_column(Text, default=None, nullable=True)
    ai_audit_summary: Mapped[str] = mapped_column(String(600))
    draft_invoice_note: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    line_items_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    leash_decision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, default=None, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_STAMP, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        _STAMP, default=utcnow, onupdate=utcnow
    )


class RemediationInvoice(Base):
    __tablename__ = "remediation_invoices"
    __table_args__ = (UniqueConstraint("discrepancy_id", "attempt"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    discrepancy_id: Mapped[str] = mapped_column(String(32), ForeignKey("discrepancies.id"))
    attempt: Mapped[int] = mapped_column(default=1)
    paypal_invoice_id: Mapped[str] = mapped_column(String(64), unique=True)
    # FD-<last 10 of the discrepancy ULID>-R<attempt>; never a counter (PIT-11)
    paypal_invoice_number: Mapped[str] = mapped_column(String(32), unique=True)
    recipient_email: Mapped[str] = mapped_column(String(254))
    total_amount: Mapped[Decimal] = mapped_column(_MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    invoice_view_url: Mapped[str | None] = mapped_column(Text, default=None, nullable=True)
    qr_code_base64: Mapped[str | None] = mapped_column(Text, default=None, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    sent_by: Mapped[str | None] = mapped_column(String(16), default=None, nullable=True)
    paypal_request_id: Mapped[str] = mapped_column(String(64), unique=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(_STAMP, default=None, nullable=True)
    paid_at: Mapped[dt.datetime | None] = mapped_column(_STAMP, default=None, nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PayPal WH- id
    event_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), default=None, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[dt.datetime] = mapped_column(_STAMP, default=utcnow)
    processed_at: Mapped[dt.datetime | None] = mapped_column(
        _STAMP, default=None, nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
