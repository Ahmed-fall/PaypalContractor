"""Leash tables (plan A.3, P5) on the shared SQLAlchemy ``Base``.

Rulings that deviate from A.3 (all deliberate):
- ``payroll_lines.hours`` is ``Numeric(7,2)``: ``(5,2)`` overflows above 999.99.
  ``timesheet_entries.hours`` is also ``Numeric(7,2)``, consistent with it.
- ``timesheet_uploads.table_json`` keeps the parsed raw table until its mapping is
  confirmed.
- ``payroll_runs.idempotency_key`` (UNIQUE) makes run creation replay-safe.
- ``payroll_runs.warnings_json`` lists run warnings, e.g. unknown members as
  ``{"code": "PR-05", "member": "<escaped name>"}``.

The decisions table is append-only: nothing in the code base changes or removes a row.
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
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db.database import Base, utcnow

_MONEY = Numeric(12, 2)
_RATE = Numeric(8, 2)
_TS = DateTime(timezone=True)
_EMPTY_LIST = text("'[]'")


class LeashPolicyRow(Base):
    __tablename__ = "leash_policies"
    __table_args__ = (
        Index(
            "one_active_policy",
            "active",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    source_text: Mapped[str] = mapped_column(Text)
    compiled_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(_TS, default=utcnow)
    activated_at: Mapped[dt.datetime | None] = mapped_column(_TS, nullable=True)


class LeashDecisionRow(Base):
    __tablename__ = "leash_decisions"
    __table_args__ = (
        CheckConstraint("subject_type IN ('discrepancy','payroll_line','payroll_run')"),
        CheckConstraint("decision IN ('ALLOW','ASK','DENY')"),
        CheckConstraint("actor IN ('leash','owner')"),
        Index("ix_decisions_subject", "subject_type", "subject_id"),
        Index("ix_decisions_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    policy_version: Mapped[int] = mapped_column(Integer, ForeignKey("leash_policies.version"))
    action_type: Mapped[str] = mapped_column(String(32))
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(8))
    rule_ids_json: Mapped[list[str]] = mapped_column(JSON)
    facts_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    reason: Mapped[str] = mapped_column(String(200))
    actor: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = mapped_column(_TS, default=utcnow)


class TeamMemberRow(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        CheckConstraint("hourly_rate > 0"),
        Index(
            "one_active_email",
            "payout_email",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    payout_email: Mapped[str] = mapped_column(String(254))
    email_changed_at: Mapped[dt.datetime | None] = mapped_column(_TS, nullable=True)
    hourly_rate: Mapped[Decimal] = mapped_column(_RATE)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    first_paid_at: Mapped[dt.datetime | None] = mapped_column(_TS, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(_TS, default=utcnow)


class TimesheetUploadRow(Base):
    __tablename__ = "timesheet_uploads"
    __table_args__ = (CheckConstraint("status IN ('MAPPING_REQUIRED','PARSED','REJECTED')"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64), unique=True)
    header_signature: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    error_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    table_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_TS, default=utcnow)


class ColumnMappingRow(Base):
    __tablename__ = "column_mappings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    header_signature: Mapped[str] = mapped_column(String(64), unique=True)
    mapping_json: Mapped[dict[str, str]] = mapped_column(JSON)
    confirmed_at: Mapped[dt.datetime] = mapped_column(_TS, default=utcnow)


class TimesheetEntryRow(Base):
    __tablename__ = "timesheet_entries"
    __table_args__ = (Index("ix_entries_ms_member", "milestone_code", "team_member_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    upload_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("timesheet_uploads.id", ondelete="CASCADE")
    )
    team_member_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("team_members.id"), nullable=True
    )
    raw_member_name: Mapped[str] = mapped_column(String(120))
    work_date: Mapped[dt.date] = mapped_column(Date)
    hours: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    milestone_code: Mapped[str] = mapped_column(String(16))
    rate_in_file: Mapped[Decimal | None] = mapped_column(_RATE, nullable=True)


class PayrollRunRow(Base):
    __tablename__ = "payroll_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('MILESTONE','MONTHLY')"),
        CheckConstraint(
            "status IN ('PREPARED','CHECKED','AWAITING_APPROVAL','SENT','PAID','PARTIAL','FAILED')"
        ),
        CheckConstraint("milestone_id IS NOT NULL OR period IS NOT NULL"),
        # One open (unsent) run per milestone, enforced by the database.
        Index(
            "one_open_run_per_milestone",
            "milestone_id",
            unique=True,
            postgresql_where=text("status IN ('PREPARED','CHECKED','AWAITING_APPROVAL')"),
            sqlite_where=text("status IN ('PREPARED','CHECKED','AWAITING_APPROVAL')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))
    milestone_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("milestones.id"), nullable=True
    )
    period: Mapped[str | None] = mapped_column(String(7), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    total: Mapped[Decimal] = mapped_column(_MONEY)
    cash_in_snapshot: Mapped[Decimal] = mapped_column(_MONEY)
    cash_used_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    run_decision_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("leash_decisions.id"), nullable=True
    )
    sender_batch_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    paypal_batch_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    batch_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    warnings_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, server_default=_EMPTY_LIST
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(_TS, nullable=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(_TS, nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(_TS, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(_TS, default=utcnow)


class PayrollLineRow(Base):
    __tablename__ = "payroll_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "team_member_id"),
        CheckConstraint("decision IN ('ALLOW','ASK','DENY')"),
        CheckConstraint("owner_action IN ('APPROVED','EXCLUDED')"),
        CheckConstraint("payout_status IN ('PENDING','SUCCESS','UNCLAIMED','FAILED')"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("payroll_runs.id", ondelete="CASCADE")
    )
    team_member_id: Mapped[str] = mapped_column(String(32), ForeignKey("team_members.id"))
    hours: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    rate: Mapped[Decimal] = mapped_column(_RATE)
    gross: Mapped[Decimal] = mapped_column(_MONEY)
    decision: Mapped[str] = mapped_column(String(8))
    decision_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("leash_decisions.id"), nullable=True
    )
    flags_json: Mapped[list[str]] = mapped_column(JSON, default=list, server_default=_EMPTY_LIST)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    owner_action: Mapped[str | None] = mapped_column(String(10), nullable=True)
    owner_action_at: Mapped[dt.datetime | None] = mapped_column(_TS, nullable=True)
    # The line's flags at the moment of the owner action: an approval covers only these.
    owner_flags_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    payout_item_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    paypal_transaction_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    payout_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(_TS, default=utcnow, onupdate=utcnow)


LEASH_TABLES: list[Table] = [
    LeashPolicyRow.__table__,  # type: ignore[list-item]
    LeashDecisionRow.__table__,  # type: ignore[list-item]
    TeamMemberRow.__table__,  # type: ignore[list-item]
    TimesheetUploadRow.__table__,  # type: ignore[list-item]
    ColumnMappingRow.__table__,  # type: ignore[list-item]
    TimesheetEntryRow.__table__,  # type: ignore[list-item]
    PayrollRunRow.__table__,  # type: ignore[list-item]
    PayrollLineRow.__table__,  # type: ignore[list-item]
]
