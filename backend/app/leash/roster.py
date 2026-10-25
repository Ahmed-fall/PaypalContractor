"""Team roster rules for payroll.

``email_changed_at`` is set by the server and only when the normalised payout email
actually changes; PR-01 (payroll diversion) relies on it. Models are immutable, and
updates return new instances. No I/O and no clock reads: ``now`` is always passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from ulid import ULID

from ..schemas.contracts import TeamMemberCreate, TeamMemberOut, TeamMemberUpdate


@dataclass(frozen=True)
class TeamMember:
    member_id: str
    name: str
    payout_email: str
    hourly_rate: Decimal
    email_changed_at: datetime | None
    first_paid_at: datetime | None
    created_at: datetime
    active: bool = True
    currency: str = "USD"


class RosterConflict(Exception):
    """Another active member already uses this payout email."""


def normalise_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def create_member(
    data: TeamMemberCreate, now: datetime, member_id: str | None = None
) -> TeamMember:
    return TeamMember(
        member_id=member_id or "tm_" + str(ULID()).lower(),
        name=data.name,
        payout_email=_normalise_email(data.payout_email),
        hourly_rate=data.hourly_rate,
        email_changed_at=None,
        first_paid_at=None,
        created_at=now,
    )


def update_member(
    member: TeamMember, patch: TeamMemberUpdate, now: datetime
) -> TeamMember:
    changes: dict[str, object] = {}
    fields = patch.model_fields_set
    if "name" in fields and patch.name is not None:
        changes["name"] = patch.name
    if "hourly_rate" in fields and patch.hourly_rate is not None:
        changes["hourly_rate"] = patch.hourly_rate
    if "active" in fields and patch.active is not None:
        changes["active"] = patch.active
    if "payout_email" in fields and patch.payout_email is not None:
        new_email = _normalise_email(patch.payout_email)
        if new_email != member.payout_email:
            changes["payout_email"] = new_email
            changes["email_changed_at"] = now
    return replace(member, **changes)  # type: ignore[arg-type]


def to_out(member: TeamMember) -> TeamMemberOut:
    return TeamMemberOut(
        member_id=member.member_id,
        name=member.name,
        payout_email=member.payout_email,
        hourly_rate=member.hourly_rate,
        email_changed_at=member.email_changed_at,
        first_paid_at=member.first_paid_at,
        active=member.active,
    )


class InMemoryRoster:
    def __init__(self) -> None:
        self._members: dict[str, TeamMember] = {}

    def _check_conflict(self, m: TeamMember) -> None:
        if not m.active:
            return
        for other in self._members.values():
            if (
                other.member_id != m.member_id
                and other.active
                and other.payout_email == m.payout_email
            ):
                msg = "another active member already uses this payout email"
                raise RosterConflict(msg)

    def add(self, m: TeamMember) -> None:
        self._check_conflict(m)
        self._members[m.member_id] = m

    def replace(self, m: TeamMember) -> None:
        if m.member_id not in self._members:
            raise KeyError(m.member_id)
        self._check_conflict(m)
        self._members[m.member_id] = m

    def get(self, member_id: str) -> TeamMember | None:
        return self._members.get(member_id)

    def active(self) -> tuple[TeamMember, ...]:
        return tuple(m for m in self._members.values() if m.active)

    def find_by_name(self, raw_name: str) -> TeamMember | None:
        key = normalise_name(raw_name)
        matches = [m for m in self.active() if normalise_name(m.name) == key]
        return matches[0] if len(matches) == 1 else None
