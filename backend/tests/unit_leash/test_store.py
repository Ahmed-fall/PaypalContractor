"""Leash tables, SQL stores and the demo seed, on a temporary SQLite database."""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="fd-leash-"), "default.db"),
)
os.environ.setdefault("PAYPAL_CLIENT_ID", "test-client-id")
os.environ.setdefault("PAYPAL_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("DEMO_KEY", "test-demo-key-0123456789abcdef")
os.environ.setdefault("PAYPAL_API_BASE_URL", "https://api-m.sandbox.paypal.com")
os.environ.setdefault("PAYPAL_WEBHOOK_ID", "wh-test-id")

from collections.abc import Callable, Iterator  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, event, func, inspect, select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from backend.app.db import models as detective_models  # noqa: E402,F401
from backend.app.db.database import Base  # noqa: E402
from backend.app.leash import models as lm  # noqa: E402
from backend.app.leash.policy import default_policy  # noqa: E402
from backend.app.leash.roster import (  # noqa: E402
    RosterConflict,
    TeamMember,
    create_member,
    update_member,
)
from backend.app.leash.seed import seed_leash_demo  # noqa: E402
from backend.app.leash.service import LeashService  # noqa: E402
from backend.app.leash.store import (  # noqa: E402
    SqlDecisionLog,
    SqlMappingStore,
    SqlPolicyProvider,
    SqlRoster,
    aware,
    from_team_member,
    to_team_member,
)
from backend.app.schemas.contracts import (  # noqa: E402
    GuardAction,
    TeamMemberCreate,
    TeamMemberUpdate,
)

D = Decimal
NOW = datetime(2026, 11, 4, 18, 40, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'leash.db'}", future=True)

    @event.listens_for(eng, "connect")
    def _pragma(conn: Any, _rec: Any) -> None:
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def sf(engine: Engine) -> Callable[[], Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _count(sf: Callable[[], Session], model: Any) -> int:
    with sf() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def test_tables_create_on_sqlite(engine: Engine) -> None:
    insp = inspect(engine)
    names = set(insp.get_table_names())
    assert {
        "leash_policies",
        "leash_decisions",
        "team_members",
        "timesheet_uploads",
        "column_mappings",
        "timesheet_entries",
        "payroll_runs",
        "payroll_lines",
    } <= names
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name, sql FROM sqlite_master WHERE type='index'")).all()
    by_name = {r[0]: (r[1] or "") for r in rows}
    assert "WHERE active = 1" in by_name["one_active_policy"]
    assert "WHERE active = 1" in by_name["one_active_email"]
    assert "UNIQUE" in by_name["one_active_email"].upper()


@pytest.mark.asyncio
async def test_decision_log_appends_one_row(sf: Callable[[], Session]) -> None:
    provider = SqlPolicyProvider(sf)
    service = LeashService(provider, SqlDecisionLog(sf))
    result = await service.decide(
        GuardAction.SEND_REMEDIATION_INVOICE,
        {"amount": D("200.00"), "invoice_total": D("200.00")},
        "dsc_1",
    )
    assert _count(sf, lm.LeashDecisionRow) == 1
    with sf() as db:
        row = db.scalars(select(lm.LeashDecisionRow)).one()
    assert row.id == result.decision_id
    assert row.policy_version == 1
    assert row.subject_type == "discrepancy"
    assert row.subject_id == "dsc_1"
    assert row.decision == result.decision.value
    assert row.actor == "leash"
    assert row.amount == D("200.00")
    assert isinstance(row.facts_json, dict)


@pytest.mark.asyncio
async def test_policy_provider_seeds_v1_once(sf: Callable[[], Session]) -> None:
    provider = SqlPolicyProvider(sf)
    first = await provider.active_policy()
    second = await provider.active_policy()
    assert first.version == second.version == 1
    assert first == default_policy()
    assert _count(sf, lm.LeashPolicyRow) == 1
    with sf() as db:
        row = db.scalars(select(lm.LeashPolicyRow)).one()
    assert row.active is True
    assert row.activated_at is not None


def _create(name: str, email: str, rate: str = "25.00") -> TeamMember:
    return create_member(TeamMemberCreate(name=name, payout_email=email, hourly_rate=D(rate)), NOW)


def test_sql_roster_matches_in_memory_rules(sf: Callable[[], Session]) -> None:
    roster = SqlRoster(sf)
    omar = _create("Omar", "  Omar@Example.com ")
    roster.add(omar)
    got = roster.get(omar.member_id)
    assert got is not None
    assert got.payout_email == "omar@example.com"
    assert got.email_changed_at is None
    assert got.created_at.tzinfo is not None

    # email_changed_at only on a real change
    same = update_member(got, TeamMemberUpdate(payout_email="OMAR@example.com"), NOW)
    roster.replace(same)
    again = roster.get(omar.member_id)
    assert again is not None and again.email_changed_at is None
    changed = update_member(again, TeamMemberUpdate(payout_email="new@example.com"), NOW)
    roster.replace(changed)
    final = roster.get(omar.member_id)
    assert final is not None
    assert final.email_changed_at == NOW
    assert final.payout_email == "new@example.com"

    # conflicts
    with pytest.raises(RosterConflict):
        roster.add(_create("Someone Else", "new@example.com"))
    with pytest.raises(RosterConflict):
        roster.add(_create("  omar ", "other@example.com"))
    with pytest.raises(KeyError):
        roster.replace(_create("Ghost", "ghost@example.com"))

    # lookups and active()
    roster.add(_create("Sara", "sara@example.com", "22.00"))
    assert roster.find_by_name("SARA ") is not None
    assert roster.find_by_key("sara") is not None
    assert roster.find_by_name("nobody") is None
    assert {m.name for m in roster.active()} == {"Omar", "Sara"}

    # deactivated members free their email and name
    roster.replace(update_member(final, TeamMemberUpdate(active=False), NOW))
    assert {m.name for m in roster.active()} == {"Sara"}
    roster.add(_create("Omar", "new@example.com"))


def test_sql_roster_ambiguous_name_is_unknown(sf: Callable[[], Session]) -> None:
    # Two active rows with one normalised name cannot come through add(); force them.
    with sf() as db:
        for i in (1, 2):
            db.add(
                from_team_member(
                    TeamMember(
                        member_id=f"tm_{i}",
                        name="Sam Lee",
                        payout_email=f"sam{i}@example.com",
                        hourly_rate=D("10.00"),
                        email_changed_at=None,
                        first_paid_at=None,
                        created_at=NOW,
                    )
                )
            )
        db.commit()
    roster = SqlRoster(sf)
    assert roster.find_by_name("sam lee") is None
    assert len(roster.active()) == 2


def test_member_mappers_round_trip() -> None:
    m = _create("Omar", "omar@example.com")
    assert to_team_member(from_team_member(m)) == m


@pytest.mark.asyncio
async def test_mapping_store_roundtrip(sf: Callable[[], Session]) -> None:
    store = SqlMappingStore(sf)
    assert await store.get("sig") is None
    await store.put("sig", {"Employee": "member", "Hrs": "hours"})
    assert await store.get("sig") == {"Employee": "member", "Hrs": "hours"}
    await store.put("sig", {"Name": "member"})
    assert await store.get("sig") == {"Name": "member"}
    assert _count(sf, lm.ColumnMappingRow) == 1


def test_seed_is_idempotent(sf: Callable[[], Session]) -> None:
    tables = (
        lm.TeamMemberRow,
        lm.LeashPolicyRow,
        lm.ColumnMappingRow,
        lm.TimesheetUploadRow,
        lm.TimesheetEntryRow,
    )
    with sf() as db:
        seed_leash_demo(db, now=NOW)
    first = [_count(sf, t) for t in tables]
    with sf() as db:
        seed_leash_demo(db, now=NOW)
    assert [_count(sf, t) for t in tables] == first
    assert first[0] == 3 and first[1] == 1 and first[2] == 1 and first[3] == 1
    assert first[4] == 9

    roster = SqlRoster(sf)
    omar = roster.find_by_name("Omar")
    assert omar is not None
    assert omar.payout_email == "omar-test-account@personal.example.com"
    assert omar.email_changed_at == NOW - timedelta(days=1)
    assert omar.first_paid_at == datetime(2026, 9, 30, tzinfo=UTC)
    sara = roster.find_by_name("Sara")
    youssef = roster.find_by_name("Youssef")
    assert sara is not None and youssef is not None
    assert (sara.hourly_rate, youssef.hourly_rate) == (D("22.00"), D("25.00"))
    assert sara.first_paid_at is not None and youssef.first_paid_at is not None

    with sf() as db:
        hours: dict[str, Decimal] = {}
        for e in db.scalars(select(lm.TimesheetEntryRow)):
            hours[e.raw_member_name] = hours.get(e.raw_member_name, D(0)) + e.hours
            assert e.milestone_code == "MS-01"
            assert e.team_member_id is not None
        upload = db.scalars(select(lm.TimesheetUploadRow)).one()
        assert upload.status == "PARSED"
        mapping = db.scalars(select(lm.ColumnMappingRow)).one()
        assert mapping.mapping_json == {
            "Employee": "member",
            "Day": "work_date",
            "Hrs": "hours",
            "Project": "milestone_code",
        }
    assert hours == {"Omar": D("12"), "Sara": D("10"), "Youssef": D("8")}


def test_aware() -> None:
    naive = datetime(2026, 11, 4, 12, 0)
    assert aware(naive) == datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    already = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert aware(already) is already
    assert aware(None) is None


def test_default_service_is_sql_backed() -> None:
    from backend.app.leash import service as svc

    built = svc._default_service()
    assert isinstance(built._policies, SqlPolicyProvider)
    assert isinstance(built._log, SqlDecisionLog)


# ---- Fix round 1 -------------------------------------------------------------


def _fake_db_module(exc: Exception) -> Any:
    import types

    class _Boom(types.ModuleType):
        def __getattr__(self, name: str) -> Any:
            raise exc

    return _Boom("backend.app.db.database")


def test_default_service_propagates_non_import_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from backend.app.leash import service as svc

    monkeypatch.setattr(svc, "_default", None)
    monkeypatch.setitem(sys.modules, "backend.app.db.database", _fake_db_module(ValueError("x")))
    with pytest.raises(ValueError):
        svc._default_service()
    assert svc._default is None


def test_default_service_falls_back_only_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from backend.app.leash import service as svc

    monkeypatch.setattr(svc, "_default", None)
    monkeypatch.setitem(sys.modules, "backend.app.db.database", None)
    built = svc._default_service()
    assert isinstance(built._policies, svc.StaticPolicyProvider)
    assert isinstance(built._log, svc.InMemoryDecisionLog)


def test_default_service_is_one_object_across_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    from backend.app.leash import service as svc

    monkeypatch.setattr(svc, "_default", None)
    real = svc._build_default_service
    calls: list[int] = []

    def slow() -> LeashService:
        calls.append(1)
        time.sleep(0.2)
        return real()

    monkeypatch.setattr(svc, "_build_default_service", slow)
    out: list[LeashService] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        barrier.wait()
        out.append(svc._default_service())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 2 and out[0] is out[1]
    assert len(calls) == 1


def _policy_row(version: int, active: bool) -> lm.LeashPolicyRow:
    return lm.LeashPolicyRow(
        id=f"lpol_{version}",
        version=version,
        source_text="t",
        compiled_json=default_policy()
        .model_copy(update={"version": version})
        .model_dump(mode="json"),
        active=active,
    )


@pytest.mark.asyncio
async def test_policy_provider_never_activates_a_draft(sf: Callable[[], Session]) -> None:
    with sf() as db:
        db.add_all([_policy_row(1, False), _policy_row(2, False)])
        db.commit()
    policy = await SqlPolicyProvider(sf).active_policy()
    assert policy == default_policy().model_copy(update={"version": 3})
    with sf() as db:
        rows = {r.version: r.active for r in db.scalars(select(lm.LeashPolicyRow))}
    assert rows == {1: False, 2: False, 3: True}
