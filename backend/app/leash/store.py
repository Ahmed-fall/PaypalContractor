"""SQL-backed stores for Leash: decision log, policy, roster and mapping cache.

Each class takes a ``session_factory`` so tests can point it at a temporary database.
Every method opens its own short session and commits (or rolls back) before returning.
Datetimes leave here timezone-aware UTC: SQLite hands naive values back, so reads go
through ``aware``. The decision log has no way to change or remove a row.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import overload

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.database import SessionLocal, new_id, utcnow
from ..schemas.contracts import LeashPolicy, TimesheetField
from .models import (
    ColumnMappingRow,
    LeashDecisionRow,
    LeashPolicyRow,
    TeamMemberRow,
)
from .policy import default_policy
from .roster import RosterConflict, TeamMember, normalise_name
from .service import DecisionRecord

SessionFactory = Callable[[], Session]

DEFAULT_POLICY_SOURCE = "Seeded default Leash policy (G-01 to G-05)."


@overload
def aware(value: datetime) -> datetime: ...
@overload
def aware(value: None) -> None: ...
def aware(value: datetime | None) -> datetime | None:
    """Mark a naive datetime (as SQLite returns it) as UTC; leave aware ones alone."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def to_team_member(row: TeamMemberRow) -> TeamMember:
    return TeamMember(
        member_id=row.id,
        name=row.name,
        payout_email=row.payout_email,
        hourly_rate=row.hourly_rate,
        email_changed_at=aware(row.email_changed_at),
        first_paid_at=aware(row.first_paid_at),
        created_at=aware(row.created_at),
        active=row.active,
        currency=row.currency,
    )


def _apply_member(row: TeamMemberRow, m: TeamMember) -> None:
    row.name = m.name
    row.payout_email = m.payout_email.strip().lower()
    row.email_changed_at = m.email_changed_at
    row.hourly_rate = m.hourly_rate
    row.currency = m.currency
    row.first_paid_at = m.first_paid_at
    row.active = m.active


def from_team_member(m: TeamMember) -> TeamMemberRow:
    row = TeamMemberRow(id=m.member_id, created_at=m.created_at)
    _apply_member(row, m)
    return row


class SqlDecisionLog:
    """Append-only: ``append`` inserts in its own committed session."""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._sf = session_factory

    def _insert(self, record: DecisionRecord) -> None:
        with self._sf() as db:
            db.add(
                LeashDecisionRow(
                    id=record.decision_id,
                    policy_version=record.policy_version,
                    action_type=record.action_type.value,
                    subject_type=record.subject_type,
                    subject_id=record.subject_id,
                    decision=record.decision.value,
                    rule_ids_json=list(record.rule_ids),
                    facts_json=record.facts,
                    amount=record.amount,
                    reason=record.reason,
                    actor=record.actor,
                    created_at=record.created_at,
                )
            )
            db.commit()

    async def append(self, record: DecisionRecord) -> None:
        await asyncio.to_thread(self._insert, record)


class SqlPolicyProvider:
    """The active policy row; version 1 (the default policy) is seeded on first use."""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._sf = session_factory

    def _load(self) -> LeashPolicy:
        with self._sf() as db:
            row = db.scalars(select(LeashPolicyRow).where(LeashPolicyRow.active.is_(True))).first()
            if row is None:
                row = self._bootstrap(db)
            return LeashPolicy.model_validate(row.compiled_json)

    def _bootstrap(self, db: Session) -> LeashPolicyRow:
        """No active policy: activate the highest existing version, else seed v1."""
        latest = db.scalars(select(LeashPolicyRow).order_by(LeashPolicyRow.version.desc())).first()
        if latest is None:
            return self._seed_default(db)
        latest.active = True
        latest.activated_at = utcnow()
        try:
            db.commit()
        except IntegrityError:  # another caller activated a policy first
            db.rollback()
            existing = db.scalars(
                select(LeashPolicyRow).where(LeashPolicyRow.active.is_(True))
            ).first()
            if existing is None:
                raise
            return existing
        return latest

    def _seed_default(self, db: Session) -> LeashPolicyRow:
        policy = default_policy()
        row = LeashPolicyRow(
            id=new_id("lpol"),
            version=policy.version,
            source_text=DEFAULT_POLICY_SOURCE,
            compiled_json=policy.model_dump(mode="json"),
            active=True,
            activated_at=utcnow(),
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:  # another caller seeded v1 first
            db.rollback()
            existing = db.scalars(
                select(LeashPolicyRow).where(LeashPolicyRow.active.is_(True))
            ).first()
            if existing is None:
                raise
            return existing
        return row

    async def active_policy(self) -> LeashPolicy:
        return await asyncio.to_thread(self._load)


class SqlRoster:
    """``InMemoryRoster``'s method surface, backed by ``team_members``."""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._sf = session_factory

    @staticmethod
    def _check_conflict(db: Session, m: TeamMember) -> None:
        if not m.active:
            return
        email = m.payout_email.strip().lower()
        key = normalise_name(m.name)
        others = db.scalars(
            select(TeamMemberRow).where(
                TeamMemberRow.active.is_(True), TeamMemberRow.id != m.member_id
            )
        )
        for other in others:
            if other.payout_email == email:
                msg = "another active member already uses this payout email"
                raise RosterConflict(msg)
            if normalise_name(other.name) == key:
                msg = "another active member already has this name"
                raise RosterConflict(msg)

    def add(self, m: TeamMember) -> None:
        with self._sf() as db:
            self._check_conflict(db, m)
            db.add(from_team_member(m))
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                msg = "this member conflicts with an existing one"
                raise RosterConflict(msg) from exc

    def replace(self, m: TeamMember) -> None:
        with self._sf() as db:
            row = db.get(TeamMemberRow, m.member_id)
            if row is None:
                raise KeyError(m.member_id)
            self._check_conflict(db, m)
            _apply_member(row, m)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                msg = "this member conflicts with an existing one"
                raise RosterConflict(msg) from exc

    def get(self, member_id: str) -> TeamMember | None:
        with self._sf() as db:
            row = db.get(TeamMemberRow, member_id)
            return to_team_member(row) if row is not None else None

    def active(self) -> tuple[TeamMember, ...]:
        with self._sf() as db:
            rows = db.scalars(
                select(TeamMemberRow)
                .where(TeamMemberRow.active.is_(True))
                .order_by(TeamMemberRow.created_at, TeamMemberRow.id)
            )
            return tuple(to_team_member(r) for r in rows)

    def find_by_key(self, key: str) -> TeamMember | None:
        """Active member whose normalised name equals ``key``; None if none or ambiguous."""
        matches = [m for m in self.active() if normalise_name(m.name) == key]
        return matches[0] if len(matches) == 1 else None

    def find_by_name(self, raw_name: str) -> TeamMember | None:
        return self.find_by_key(normalise_name(raw_name))


class SqlMappingStore:
    """Confirmed column mappings by header signature (``column_mappings``)."""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._sf = session_factory

    def _get(self, signature: str) -> dict[str, TimesheetField] | None:
        with self._sf() as db:
            row = db.scalars(
                select(ColumnMappingRow).where(ColumnMappingRow.header_signature == signature)
            ).first()
            if row is None:
                return None
            return dict(row.mapping_json)  # type: ignore[arg-type]

    def _put(self, signature: str, mapping: dict[str, TimesheetField]) -> None:
        with self._sf() as db:
            row = db.scalars(
                select(ColumnMappingRow).where(ColumnMappingRow.header_signature == signature)
            ).first()
            if row is None:
                db.add(
                    ColumnMappingRow(
                        id=new_id("cm"),
                        header_signature=signature,
                        mapping_json=dict(mapping),
                    )
                )
            else:
                row.mapping_json = dict(mapping)
                row.confirmed_at = utcnow()
            db.commit()

    async def get(self, signature: str) -> dict[str, TimesheetField] | None:
        return await asyncio.to_thread(self._get, signature)

    async def put(self, signature: str, mapping: dict[str, TimesheetField]) -> None:
        await asyncio.to_thread(self._put, signature, mapping)
