"""Roster rules: server-set email_changed_at, conflicts, name matching."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from backend.app.leash.roster import (
    InMemoryRoster,
    RosterConflict,
    TeamMember,
    create_member,
    normalise_name,
    to_out,
    update_member,
)
from backend.app.schemas.contracts import (
    TeamMemberCreate,
    TeamMemberOut,
    TeamMemberUpdate,
)

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "shared"
    / "fixtures"
    / "leash"
    / "team_member_omar.json"
)
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 4, 18, 40, tzinfo=UTC)


def _omar(
    member_id: str = "tm_01", email: str = "omar@example.com", name: str = "Omar"
) -> TeamMember:
    return create_member(
        TeamMemberCreate(name=name, payout_email=email, hourly_rate=Decimal("25.00")),
        T0,
        member_id=member_id,
    )


def test_create_normalises_and_has_no_change_timestamp() -> None:
    m = create_member(
        TeamMemberCreate(
            name="Omar", payout_email="  Omar@Example.COM ", hourly_rate=Decimal(25)
        ),
        T0,
    )
    assert m.payout_email == "omar@example.com"
    assert m.email_changed_at is None
    assert m.first_paid_at is None
    assert m.active is True
    assert m.currency == "USD"
    assert m.created_at == T0
    assert m.member_id.startswith("tm_")
    assert m.member_id == m.member_id.lower()


def test_email_change_sets_timestamp() -> None:
    m = _omar()
    patch = TeamMemberUpdate(payout_email="sb-omar.new@personal.example.com")
    updated = update_member(m, patch, T1)
    assert updated.payout_email == "sb-omar.new@personal.example.com"
    assert updated.email_changed_at == T1
    assert m.email_changed_at is None  # original untouched


def test_email_case_change_is_not_a_change() -> None:
    m = _omar()
    updated = update_member(m, TeamMemberUpdate(payout_email=" OMAR@Example.com"), T1)
    assert updated.email_changed_at is None
    assert updated.payout_email == "omar@example.com"


def test_other_field_changes_keep_timestamp() -> None:
    m = update_member(_omar(), TeamMemberUpdate(payout_email="new@example.com"), T1)
    later = datetime(2026, 11, 10, tzinfo=UTC)
    updated = update_member(
        m,
        TeamMemberUpdate(hourly_rate=Decimal("30.00"), name="Omar K", active=False),
        later,
    )
    assert updated.email_changed_at == T1
    assert updated.hourly_rate == Decimal("30.00")
    assert updated.name == "Omar K"
    assert updated.active is False


def test_conflicting_active_email_rejected() -> None:
    roster = InMemoryRoster()
    a = _omar("tm_a", "dup@example.com")
    roster.add(a)
    with pytest.raises(RosterConflict):
        roster.add(_omar("tm_b", "dup@example.com", "Sara"))

    b = _omar("tm_b", "other@example.com", "Sara")
    roster.add(b)
    with pytest.raises(RosterConflict):
        roster.replace(
            update_member(b, TeamMemberUpdate(payout_email="dup@example.com"), T1)
        )
    # replacing a member with itself is fine
    roster.replace(a)

    # inactive duplicates are allowed
    inactive = update_member(
        _omar("tm_c", "dup@example.com", "Youssef"),
        TeamMemberUpdate(active=False),
        T1,
    )
    roster.add(inactive)
    assert roster.get("tm_c") == inactive


def test_replace_unknown_member_raises_key_error() -> None:
    with pytest.raises(KeyError):
        InMemoryRoster().replace(_omar("tm_zz"))


def test_normalise_name() -> None:
    assert normalise_name("  Omar   KHALED ") == "omar khaled"
    assert normalise_name("Stra\u00dfe") == "strasse"


def test_find_by_key() -> None:
    roster = InMemoryRoster()
    omar = create_member(
        TeamMemberCreate(
            name="Omar Khaled", payout_email="o@example.com", hourly_rate=Decimal(20)
        ),
        T0,
        member_id="tm_1",
    )
    roster.add(omar)
    assert roster.find_by_key(normalise_name("  OMAR   khaled ")) == omar
    assert roster.find_by_key("nobody") is None


def test_find_by_name() -> None:
    roster = InMemoryRoster()
    omar = create_member(
        TeamMemberCreate(
            name="Omar Khaled", payout_email="o@example.com", hourly_rate=Decimal(20)
        ),
        T0,
        member_id="tm_1",
    )
    sara = create_member(
        TeamMemberCreate(
            name="Sara", payout_email="s@example.com", hourly_rate=Decimal(20)
        ),
        T0,
        member_id="tm_2",
    )
    roster.add(omar)
    roster.add(sara)
    assert roster.find_by_name("  omar   KHALED ") == omar
    assert roster.find_by_name("nobody") is None

    # ambiguous: two active members share a name (the roster refuses to build this,
    # but rows loaded from storage could); the name must then count as unknown
    twin = create_member(
        TeamMemberCreate(
            name="sara", payout_email="s2@example.com", hourly_rate=Decimal(20)
        ),
        T0,
        member_id="tm_3",
    )
    roster._members[twin.member_id] = twin
    assert roster.find_by_name("SARA") is None

    # inactive members never match
    roster.replace(update_member(omar, TeamMemberUpdate(active=False), T1))
    assert roster.find_by_name("Omar Khaled") is None
    assert {m.member_id for m in roster.active()} == {"tm_2", "tm_3"}


def test_to_out_matches_fixture() -> None:
    expected = TeamMemberOut.model_validate(json.loads(FIXTURE.read_text("utf-8")))
    member = TeamMember(
        member_id="tm_01",
        name="Omar",
        payout_email="sb-omar.new@personal.example.com",
        hourly_rate=Decimal("25.00"),
        email_changed_at=datetime(2026, 11, 4, 18, 40, tzinfo=UTC),
        first_paid_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        active=True,
        currency="USD",
        created_at=T0,
    )
    assert to_out(member) == expected


def _named(member_id: str, name: str, email: str) -> TeamMember:
    return create_member(
        TeamMemberCreate(name=name, payout_email=email, hourly_rate=Decimal("25.00")),
        T0,
        member_id=member_id,
    )


def test_duplicate_active_name_is_rejected() -> None:
    roster = InMemoryRoster()
    roster.add(_named("tm_a", "Sara Ali", "a@example.com"))
    with pytest.raises(RosterConflict, match="name"):
        roster.add(_named("tm_b", "  sara   ALI ", "b@example.com"))

    b = _named("tm_b", "Sara", "b@example.com")
    roster.add(b)
    with pytest.raises(RosterConflict, match="name"):
        roster.replace(update_member(b, TeamMemberUpdate(name="sara ali"), T1))


def test_inactive_member_does_not_block_its_name() -> None:
    roster = InMemoryRoster()
    a = _named("tm_a", "Sara", "a@example.com")
    roster.add(a)
    roster.replace(update_member(a, TeamMemberUpdate(active=False), T1))
    roster.add(_named("tm_b", "sara", "b@example.com"))
    roster.add(
        update_member(
            _named("tm_c", "SARA", "c@example.com"), TeamMemberUpdate(active=False), T1
        )
    )


def test_reactivating_into_a_taken_email_or_name_is_rejected() -> None:
    roster = InMemoryRoster()
    a = _named("tm_a", "Sara", "a@example.com")
    roster.add(a)
    roster.replace(update_member(a, TeamMemberUpdate(active=False), T1))
    roster.add(_named("tm_b", "Other", "a@example.com"))
    with pytest.raises(RosterConflict, match="email"):
        roster.replace(update_member(a, TeamMemberUpdate(active=True), T1))

    roster.add(_named("tm_c", "Sara", "c@example.com"))
    d = _named("tm_d", "Sara", "d@example.com")
    inactive_d = update_member(d, TeamMemberUpdate(active=False), T1)
    roster.add(inactive_d)
    with pytest.raises(RosterConflict, match="name"):
        roster.replace(update_member(inactive_d, TeamMemberUpdate(active=True), T1))
