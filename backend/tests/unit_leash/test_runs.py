"""Payroll run lifecycle, hook H2, timesheet/roster services and margin on a temp DB."""

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

import asyncio  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from backend.app.api.errors import ApiError  # noqa: E402
from backend.app.db import models as dm  # noqa: E402
from backend.app.db.database import Base  # noqa: E402
from backend.app.leash import models as lm  # noqa: E402
from backend.app.leash import service as service_module  # noqa: E402
from backend.app.leash.payroll import runs  # noqa: E402
from backend.app.leash.seed import seed_leash_demo  # noqa: E402
from backend.app.leash.service import LeashService, configure  # noqa: E402
from backend.app.leash.store import SqlDecisionLog, SqlPolicyProvider  # noqa: E402
from backend.app.schemas.contracts import (  # noqa: E402
    GuardResult,
    LeashDecision,
    TeamMemberCreate,
    TeamMemberUpdate,
    TimesheetMappingRequired,
    TimesheetParsed,
)

D = Decimal
UTC_NOW = datetime.now(UTC)
SessionMaker = Callable[[], Session]


@pytest.fixture
def sf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SessionMaker]:
    eng: Engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}", future=True)

    @event.listens_for(eng, "connect")
    def _pragma(conn: Any, _rec: Any) -> None:
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    monkeypatch.setattr(runs, "session_factory", factory)
    monkeypatch.setattr(runs, "_load_mapper", lambda: None)
    runs._locks.clear()
    configure(
        LeashService(SqlPolicyProvider(factory), SqlDecisionLog(factory), explainer=None)
    )
    yield factory
    service_module._service = None
    eng.dispose()


def _contract_and_milestone(
    db: Session, *, status: str = "ACTIVE", ms_id: str = "ms_01", code: str = "MS-01"
) -> None:
    if db.get(dm.Contract, "cnt_01") is None:
        db.add(
            dm.Contract(
                id="cnt_01",
                title="Website build",
                client_name="Acme",
                client_paypal_email="client@example.com",
                currency="USD",
                raw_text="x",
                penalty_clause_quote="q",
                extraction_confidence=D("0.90"),
                status=status,
            )
        )
        db.flush()
    db.add(
        dm.Milestone(
            id=ms_id,
            contract_id="cnt_01",
            code=code,
            title="Design",
            expected_amount=D("1000.00"),
            due_date=date(2026, 11, 1),
            source_clause_quote="q",
        )
    )
    db.flush()


def _capture(db: Session, amount: str, cap_id: str = "cap_01", currency: str = "USD") -> None:
    db.add(
        dm.PaypalCapture(
            id=cap_id,
            milestone_id="ms_01",
            paypal_order_id="ORD" + cap_id,
            paypal_capture_id="PP" + cap_id,
            gross_amount=D(amount),
            currency=currency,
            captured_at=UTC_NOW,
            source="sandbox",
            raw_payload_json={},
        )
    )


@pytest.fixture
def demo(sf: SessionMaker) -> SessionMaker:
    with sf() as db:
        seed_leash_demo(db, now=UTC_NOW)
        _contract_and_milestone(db)
        _capture(db, "850.00")
        db.commit()
    return sf


def _count(sf: SessionMaker, model: Any) -> int:
    with sf() as db:
        return len(db.scalars(select(model)).all())


async def _prepared(sf: SessionMaker) -> str:
    run_id = await runs.on_milestone_payment("ms_01")
    assert run_id is not None
    await runs.wait_for_background()
    return run_id


def _line(out: Any, name: str) -> Any:
    return next(ln for ln in out.lines if ln.member_name == name)


@pytest.mark.asyncio
async def test_demo_run_end_to_end_on_db(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    out = runs.get_run_out(run_id)

    assert out.status == "AWAITING_APPROVAL"
    assert (out.total, out.cash_used_pct, out.cash_in) == (D("720.00"), D("84.71"), D("850.00"))
    assert out.run_flags == ["PR-06"]
    assert out.run_decision == LeashDecision.ASK
    omar, sara, youssef = (_line(out, n) for n in ("Omar", "Sara", "Youssef"))
    assert (omar.decision, omar.flags) == (LeashDecision.ASK, ["PR-01"])
    assert omar.reason
    assert omar.hours == D("12.00") and omar.gross == D("300.00")
    for line in (sara, youssef):
        assert line.decision == LeashDecision.ALLOW and line.reason is None
    assert _count(demo, lm.LeashDecisionRow) == 4


@pytest.mark.asyncio
async def test_exclude_omar_clears_pr06(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    omar = _line(runs.get_run_out(run_id), "Omar")

    out = await runs.set_line_action(run_id, omar.line_id, "EXCLUDED", "Confirm new email first")

    assert out.status == "CHECKED"
    assert (out.total, out.cash_used_pct) == (D("420.00"), D("49.41"))
    assert out.run_flags == [] and out.run_decision == LeashDecision.ALLOW
    assert _line(out, "Omar").owner_action == "EXCLUDED"
    with demo() as db:
        owner_rows = db.scalars(
            select(lm.LeashDecisionRow).where(lm.LeashDecisionRow.actor == "owner")
        ).all()
    assert len(owner_rows) == 1
    row = owner_rows[0]
    assert (row.decision, row.rule_ids_json, row.subject_id) == ("DENY", ["OWNER"], omar.line_id)
    assert row.subject_type == "payroll_line"
    assert _count(demo, lm.LeashDecisionRow) == 4 + 1 + 4  # check, owner row, re-check


@pytest.mark.asyncio
async def test_on_milestone_payment_is_idempotent(demo: SessionMaker) -> None:
    first = await _prepared(demo)
    second = await runs.on_milestone_payment("ms_01")
    await runs.wait_for_background()

    assert second == first
    assert _count(demo, lm.PayrollRunRow) == 1
    assert _count(demo, lm.PayrollLineRow) == 3
    assert _count(demo, lm.LeashDecisionRow) == 4  # no second check


@pytest.mark.asyncio
async def test_no_timesheet_hours_creates_nothing(sf: SessionMaker) -> None:
    with sf() as db:
        _contract_and_milestone(db)
        db.commit()
    assert await runs.on_milestone_payment("ms_01") is None
    assert _count(sf, lm.PayrollRunRow) == 0


@pytest.mark.asyncio
async def test_new_run_after_previous_one_was_sent(demo: SessionMaker) -> None:
    first = await _prepared(demo)
    with demo() as db:
        run = db.get(lm.PayrollRunRow, first)
        assert run is not None
        run.status = "SENT"
        db.commit()
    second = await runs.on_milestone_payment("ms_01")
    await runs.wait_for_background()
    assert second is not None and second != first


@pytest.mark.asyncio
async def test_unknown_member_flags_run(demo: SessionMaker) -> None:
    with demo() as db:
        upload = db.scalars(select(lm.TimesheetUploadRow)).first()
        assert upload is not None
        db.add(
            lm.TimesheetEntryRow(
                id="tse_zed",
                upload_id=upload.id,
                team_member_id=None,
                raw_member_name="Zed Stranger",
                work_date=date(2026, 11, 4),
                hours=D("5.00"),
                milestone_code="MS-01",
            )
        )
        db.commit()
    run_id = await _prepared(demo)

    out = runs.get_run_out(run_id)
    assert len(out.lines) == 3  # no line for the unknown member
    assert "PR-05" in out.run_flags and "PR-06" in out.run_flags
    assert out.status == "AWAITING_APPROVAL"
    with demo() as db:
        run = db.get(lm.PayrollRunRow, run_id)
        assert run is not None
        assert run.warnings_json == [{"code": "PR-05", "member": "Zed Stranger"}]


@pytest.mark.asyncio
async def test_lines_ready_to_send_409_lists_open_asks(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    omar = _line(runs.get_run_out(run_id), "Omar")

    with pytest.raises(ApiError) as caught:
        await runs.lines_ready_to_send(run_id, [])
    err = caught.value
    assert err.status_code == 409 and err.code == "LEASH_ASK_UNRESOLVED"
    assert err.message == "1 line(s) still need your decision before the team can be paid."
    assert err.details == {
        "open_asks": [{"line_id": omar.line_id, "flags": ["PR-01"]}],
        "open_run_flags": ["PR-06"],
    }

    # The line is resolved but the run flag is not: still a 409.
    await runs.set_line_action(run_id, omar.line_id, "APPROVED", None)
    with pytest.raises(ApiError) as again:
        await runs.lines_ready_to_send(run_id, [])
    assert again.value.details == {"open_asks": [], "open_run_flags": ["PR-06"]}

    ready = await runs.lines_ready_to_send(run_id, ["PR-06"])
    assert sorted(ln.member_name for ln in ready) == ["Omar", "Sara", "Youssef"]
    with demo() as db:
        run = db.get(lm.PayrollRunRow, run_id)
        assert run is not None and run.approved_at is not None


@pytest.mark.asyncio
async def test_excluded_and_denied_lines_are_never_ready(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    omar = _line(runs.get_run_out(run_id), "Omar")
    await runs.set_line_action(run_id, omar.line_id, "EXCLUDED", None)
    ready = await runs.lines_ready_to_send(run_id, [])
    assert sorted(ln.member_name for ln in ready) == ["Sara", "Youssef"]


@pytest.mark.asyncio
async def test_run_not_sendable_after_sent(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    omar = _line(runs.get_run_out(run_id), "Omar")
    with demo() as db:
        run = db.get(lm.PayrollRunRow, run_id)
        assert run is not None
        run.status = "SENT"
        db.commit()

    with pytest.raises(ApiError) as sent:
        await runs.lines_ready_to_send(run_id, ["PR-06"])
    assert (sent.value.status_code, sent.value.code) == (409, "RUN_NOT_SENDABLE")
    with pytest.raises(ApiError) as action:
        await runs.set_line_action(run_id, omar.line_id, "EXCLUDED", None)
    assert action.value.code == "RUN_NOT_SENDABLE"
    assert runs.get_run_out(run_id).status == "SENT"  # untouched


@pytest.mark.asyncio
async def test_recompute_run_status(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)

    def mark(statuses: dict[str, str], *, batch: str | None = None, status: str = "SENT") -> None:
        with demo() as db:
            run = db.get(lm.PayrollRunRow, run_id)
            assert run is not None
            run.status, run.batch_status = status, batch
            for row in db.scalars(select(lm.PayrollLineRow)):
                member = db.get(lm.TeamMemberRow, row.team_member_id)
                assert member is not None
                row.payout_status = statuses.get(member.name)
            db.commit()

    assert runs.recompute_run_status(run_id).status == "AWAITING_APPROVAL"  # not sent: unchanged

    mark({"Omar": "SUCCESS", "Sara": "SUCCESS", "Youssef": "PENDING"})
    assert runs.recompute_run_status(run_id).status == "SENT"
    mark({"Omar": "SUCCESS", "Sara": "SUCCESS", "Youssef": "SUCCESS"})
    assert runs.recompute_run_status(run_id).status == "PAID"
    with demo() as db:
        run = db.get(lm.PayrollRunRow, run_id)
        assert run is not None and run.completed_at is not None
    mark({"Omar": "SUCCESS", "Sara": "FAILED", "Youssef": "SUCCESS"}, status="SENT")
    assert runs.recompute_run_status(run_id).status == "PARTIAL"
    mark({"Omar": "PENDING", "Sara": "PENDING"}, batch="DENIED")
    out = runs.recompute_run_status(run_id)
    assert out.status == "FAILED"
    assert {ln.payout_status for ln in out.lines if ln.payout_status} == {"FAILED"}


def test_margin_rows_none_status_for_milestone_without_run(sf: SessionMaker) -> None:
    with sf() as db:
        _contract_and_milestone(db)
        _capture(db, "850.00")
        db.commit()
        result = runs.margin_rows(db)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.payroll_status == "NONE" and row.milestone_code == "MS-01"
    assert (row.cash_in, row.paid_to_team, row.pending_to_team) == (D("850.00"), D(0), D(0))
    assert (row.margin, row.margin_pct) == (D("850.00"), D("100.00"))
    assert result.summary.cash_in == D("850.00")


@pytest.mark.asyncio
async def test_margin_rows_after_payout(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    with demo() as db:
        run = db.get(lm.PayrollRunRow, run_id)
        assert run is not None
        run.status = "SENT"
        for row in db.scalars(select(lm.PayrollLineRow)):
            member = db.get(lm.TeamMemberRow, row.team_member_id)
            assert member is not None
            row.payout_status = {"Omar": "PENDING"}.get(member.name, "SUCCESS")
        db.commit()
        result = runs.margin_rows(db)
    row = result.rows[0]
    assert (row.paid_to_team, row.pending_to_team) == (D("420.00"), D("300.00"))
    assert (row.margin, row.margin_pct) == (D("430.00"), D("50.59"))
    assert row.payroll_status == "SENT"
    assert result.summary.margin == D("430.00")


def test_margin_rows_skips_milestones_of_inactive_contracts(sf: SessionMaker) -> None:
    with sf() as db:
        _contract_and_milestone(db, status="DRAFT")
        db.commit()
        assert runs.margin_rows(db).rows == []


def test_cash_in_counts_paid_invoices(sf: SessionMaker) -> None:
    with sf() as db:
        _contract_and_milestone(db)
        _capture(db, "850.00", "cap_01")
        _capture(db, "99.00", "cap_02", currency="EUR")  # other currency: ignored
        db.flush()
        for n, status, amount in ((1, "PAID", "200.00"), (2, "SENT", "75.00")):
            db.add(
                dm.Discrepancy(
                    id=f"dsc_{n}",
                    milestone_id="ms_01",
                    capture_id=f"cap_0{n}",
                    discrepancy_type="UNDERPAYMENT",
                    shortfall_amount=D(amount),
                    penalty_amount=D("0.00"),
                    total_remediation_amount=D(amount),
                    ai_audit_summary="s",
                )
            )
            db.flush()
            db.add(
                dm.RemediationInvoice(
                    id=f"inv_{n}",
                    discrepancy_id=f"dsc_{n}",
                    paypal_invoice_id=f"INV2-{n}",
                    paypal_invoice_number=f"FD-{n}",
                    recipient_email="client@example.com",
                    total_amount=D(amount),
                    currency="USD",
                    status=status,
                    paypal_request_id=f"req-{n}",
                )
            )
        db.commit()
        assert runs.cash_in_for_milestone(db, "ms_01") == D("1050.00")
        assert runs.margin_rows(db).rows[0].invoiced_open == D("75.00")


CSV_KNOWN = b"Employee,Day,Hrs,Project\nOmar,2026-12-01,4,MS-02\nNew Hire,2026-12-01,2,MS-02\n"
CSV_UNKNOWN = b"Who,When,Time worked,Task\nSara,2026-12-02,6,MS-02\n"


@pytest.mark.asyncio
async def test_upload_known_layout_parses_and_unknown_needs_mapping(demo: SessionMaker) -> None:
    with demo() as db:
        parsed = await runs.upload_timesheet(db, CSV_KNOWN, "ms02.csv")
        assert isinstance(parsed, TimesheetParsed)
        assert (parsed.entries, parsed.unknown_members, parsed.milestones) == (
            2,
            ["New Hire"],
            ["MS-02"],
        )

        pending = await runs.upload_timesheet(db, CSV_UNKNOWN, "other.csv")
        assert isinstance(pending, TimesheetMappingRequired)
        assert pending.headers == ["Who", "When", "Time worked", "Task"]
        assert pending.row_count == 1
        assert set(pending.proposal.mapping) <= set(pending.headers)
        before = len(db.scalars(select(lm.TimesheetEntryRow)).all())

        done = await runs.confirm_timesheet_mapping(
            db,
            pending.upload_id,
            {
                "Who": "member",
                "When": "work_date",
                "Time worked": "hours",
                "Task": "milestone_code",
            },
        )
        assert (done.status, done.entries, done.unknown_members) == ("PARSED", 1, [])
        assert len(db.scalars(select(lm.TimesheetEntryRow)).all()) == before + 1
        upload = db.get(lm.TimesheetUploadRow, pending.upload_id)
        assert upload is not None and upload.status == "PARSED" and upload.table_json is None
        # The confirmed layout is remembered: the same layout now parses directly.
        again = await runs.upload_timesheet(
            db, b"Who,When,Time worked,Task\nSara,2026-12-03,2,MS-02\n", "again.csv"
        )
        assert isinstance(again, TimesheetParsed)
        with pytest.raises(ApiError) as redo:
            await runs.confirm_timesheet_mapping(db, pending.upload_id, {})
        assert redo.value.status_code == 409


@pytest.mark.asyncio
async def test_duplicate_upload_409(demo: SessionMaker) -> None:
    with demo() as db:
        await runs.upload_timesheet(db, CSV_KNOWN, "a.csv")
        with pytest.raises(ApiError) as dup:
            await runs.upload_timesheet(db, CSV_KNOWN, "b.csv")
    assert (dup.value.status_code, dup.value.code) == (409, "DUPLICATE_UPLOAD")
    assert "upload_id" in (dup.value.details or {})


@pytest.mark.asyncio
async def test_timesheet_errors_map_to_http_status(demo: SessionMaker) -> None:
    with demo() as db:
        for data, name, status, code in (
            (b"x" * (2 * 1024 * 1024 + 1), "big.csv", 413, "FILE_TOO_LARGE"),
            (b"a,b", "notes.txt", 415, "UNSUPPORTED_FILE_TYPE"),
            (
                b"Employee,Day,Hrs,Project\nOmar,notadate,4,MS-02\n",
                "bad.csv",
                422,
                "TIMESHEET_INVALID",
            ),
        ):
            with pytest.raises(ApiError) as caught:
                await runs.upload_timesheet(db, data, name)
            assert (caught.value.status_code, caught.value.code) == (status, code)


def test_roster_services(sf: SessionMaker) -> None:
    with sf() as db:
        made = runs.create_member(
            db,
            TeamMemberCreate(name="Lina", payout_email="Lina@Example.com", hourly_rate=D("30.00")),
        )
        assert made.payout_email == "lina@example.com" and made.email_changed_at is None
        with pytest.raises(ApiError) as clash:
            runs.create_member(
                db,
                TeamMemberCreate(
                    name="Other", payout_email="lina@example.com", hourly_rate=D("10.00")
                ),
            )
        assert (clash.value.status_code, clash.value.code) == (409, "PAYOUT_EMAIL_IN_USE")

        changed = runs.update_member(
            db, made.member_id, TeamMemberUpdate(payout_email="lina.new@example.com")
        )
        assert changed.email_changed_at is not None  # set by the server
        assert [m.name for m in runs.list_members(db)] == ["Lina"]
        with pytest.raises(ApiError) as missing:
            runs.update_member(db, "tm_nope", TeamMemberUpdate(active=False))
        assert missing.value.status_code == 404


# --- fix round 1 -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_h2_creates_a_single_run(demo: SessionMaker) -> None:
    ids = await asyncio.gather(*[runs.on_milestone_payment("ms_01") for _ in range(6)])
    await runs.wait_for_background()

    assert len(set(ids)) == 1 and ids[0] is not None
    assert _count(demo, lm.PayrollRunRow) == 1
    assert _count(demo, lm.PayrollLineRow) == 3
    assert _count(demo, lm.LeashDecisionRow) == 4  # one check of one run


def test_database_allows_one_open_run_per_milestone(demo: SessionMaker) -> None:
    def new_run(run_id: str) -> lm.PayrollRunRow:
        return lm.PayrollRunRow(
            id=run_id,
            mode="MILESTONE",
            milestone_id="ms_01",
            status="PREPARED",
            total=D("0.00"),
            cash_in_snapshot=D("0.00"),
            cash_used_pct=D("0.00"),
        )

    with demo() as db:
        db.add(new_run("prn_a"))
        db.commit()
        db.add(new_run("prn_b"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        first = db.get(lm.PayrollRunRow, "prn_a")
        assert first is not None
        first.status = "SENT"
        db.commit()
        db.add(new_run("prn_b"))  # fine once the first is sent
        db.commit()


async def _send_first_run(demo: SessionMaker, payout_status: str | None) -> str:
    first = await _prepared(demo)
    omar = _line(runs.get_run_out(first), "Omar")
    await runs.set_line_action(first, omar.line_id, "EXCLUDED", None)
    ready = await runs.lines_ready_to_send(first, [])
    assert {ln.member_name for ln in ready} == {"Sara", "Youssef"}
    with demo() as db:
        run = db.get(lm.PayrollRunRow, first)
        assert run is not None
        run.status = "SENT"
        for row in db.scalars(select(lm.PayrollLineRow)):
            if row.owner_action != "EXCLUDED":
                row.payout_status = payout_status
        _capture(db, "500.00", "cap_02")  # the client pays the rest of MS-01
        db.commit()
    second = await runs.on_milestone_payment("ms_01")
    await runs.wait_for_background()
    assert second is not None and second != first
    return second


@pytest.mark.asyncio
@pytest.mark.parametrize("payout_status", ["PENDING", "UNCLAIMED", "SUCCESS", None])
async def test_follow_up_run_denies_unsettled_members(
    demo: SessionMaker, payout_status: str | None
) -> None:
    second = await _send_first_run(demo, payout_status)
    out = runs.get_run_out(second)
    for name in ("Sara", "Youssef"):
        line = _line(out, name)
        assert (line.decision, line.flags) == (LeashDecision.DENY, ["PR-07"])


@pytest.mark.asyncio
async def test_failed_items_may_be_paid_again(demo: SessionMaker) -> None:
    second = await _send_first_run(demo, "FAILED")
    out = runs.get_run_out(second)
    for name in ("Sara", "Youssef"):
        assert _line(out, name).decision == LeashDecision.ALLOW


@pytest.mark.asyncio
async def test_approval_expires_when_the_email_changes_again(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    omar = _line(runs.get_run_out(run_id), "Omar")
    await runs.set_line_action(run_id, omar.line_id, "APPROVED", None)
    assert len(await runs.lines_ready_to_send(run_id, ["PR-06"])) == 3

    with demo() as db:
        member = db.scalars(select(lm.TeamMemberRow).where(lm.TeamMemberRow.name == "Omar")).one()
        runs.update_member(db, member.id, TeamMemberUpdate(payout_email="omar.newer@example.com"))
    with pytest.raises(ApiError) as caught:
        await runs.lines_ready_to_send(run_id, ["PR-06"])
    assert caught.value.code == "LEASH_ASK_UNRESOLVED"
    open_asks = (caught.value.details or {})["open_asks"]
    assert [a["line_id"] for a in open_asks] == [omar.line_id]
    assert _line(runs.get_run_out(run_id), "Omar").owner_action is None


@pytest.mark.asyncio
async def test_approval_does_not_cover_a_new_flag(demo: SessionMaker) -> None:
    with demo() as db:
        sara = db.scalars(select(lm.TeamMemberRow).where(lm.TeamMemberRow.name == "Sara")).one()
        sara.first_paid_at = None  # PR-03 only
        db.commit()
    run_id = await _prepared(demo)
    out = runs.get_run_out(run_id)
    sara_line = _line(out, "Sara")
    assert sara_line.flags == ["PR-03"]
    omar = _line(out, "Omar")
    await runs.set_line_action(run_id, omar.line_id, "APPROVED", None)
    await runs.set_line_action(run_id, sara_line.line_id, "APPROVED", None)
    assert len(await runs.lines_ready_to_send(run_id, ["PR-06"])) == 3

    with demo() as db:  # PR-01 appears, but the email change predates the approval
        sara = db.scalars(select(lm.TeamMemberRow).where(lm.TeamMemberRow.name == "Sara")).one()
        sara.email_changed_at = datetime.now(UTC) - timedelta(hours=1)
        db.commit()
    with pytest.raises(ApiError) as caught:
        await runs.lines_ready_to_send(run_id, ["PR-06"])
    asks = (caught.value.details or {})["open_asks"]
    assert [a["line_id"] for a in asks] == [sara_line.line_id]
    assert sorted(asks[0]["flags"]) == ["PR-01", "PR-03"]


@pytest.mark.asyncio
async def test_guard_denied_line_does_not_count_toward_total(
    demo: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = runs.decide

    async def deny_sara(action: Any, facts: dict[str, Any], subject_id: str) -> GuardResult:
        result = await real(action, facts, subject_id)
        if facts.get("member_key") == "sara":
            return result.model_copy(update={"decision": LeashDecision.DENY})
        return result

    monkeypatch.setattr(runs, "decide", deny_sara)
    out = runs.get_run_out(await _prepared(demo))
    assert (out.total, out.cash_used_pct) == (D("500.00"), D("58.82"))


@pytest.mark.asyncio
async def test_recheck_refreshes_hours_and_gross(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    with demo() as db:
        upload = db.scalars(select(lm.TimesheetUploadRow)).first()
        sara = db.scalars(select(lm.TeamMemberRow).where(lm.TeamMemberRow.name == "Sara")).one()
        assert upload is not None
        db.add(
            lm.TimesheetEntryRow(
                id="tse_more",
                upload_id=upload.id,
                team_member_id=sara.id,
                raw_member_name="Sara",
                work_date=date(2026, 11, 5),
                hours=D("2.00"),
                milestone_code="MS-01",
            )
        )
        db.commit()
    out = await runs.check_run(run_id)
    assert (_line(out, "Sara").hours, _line(out, "Sara").gross) == (D("12.00"), D("264.00"))
    assert out.total == D("764.00")


def test_roster_conflict_kind_maps_to_codes(sf: SessionMaker) -> None:
    with sf() as db:
        runs.create_member(
            db, TeamMemberCreate(name="Lina", payout_email="a@example.com", hourly_rate=D("30.00"))
        )
        with pytest.raises(ApiError) as same_name:
            runs.create_member(
                db,
                TeamMemberCreate(name="LINA", payout_email="b@example.com", hourly_rate=D("30.00")),
            )
    assert (same_name.value.status_code, same_name.value.code) == (409, "MEMBER_NAME_IN_USE")


# --- fix round 2 -----------------------------------------------------------------------


def _add_hours(demo: SessionMaker, name: str, hours: str, day: int, entry_id: str) -> None:
    with demo() as db:
        upload = db.scalars(select(lm.TimesheetUploadRow)).first()
        member = db.scalars(select(lm.TeamMemberRow).where(lm.TeamMemberRow.name == name)).one()
        assert upload is not None
        db.add(
            lm.TimesheetEntryRow(
                id=entry_id,
                upload_id=upload.id,
                team_member_id=member.id,
                raw_member_name=name,
                work_date=date(2026, 11, day),
                hours=D(hours),
                milestone_code="MS-01",
            )
        )
        db.commit()


@pytest.mark.asyncio
async def test_gross_grows_after_approval(demo: SessionMaker) -> None:
    run_id = await _prepared(demo)
    omar = _line(runs.get_run_out(run_id), "Omar")
    await runs.set_line_action(run_id, omar.line_id, "APPROVED", None)
    assert len(await runs.lines_ready_to_send(run_id, ["PR-06"])) == 3  # unchanged: still holds

    _add_hours(demo, "Omar", "6.00", 6, "tse_p1")
    with pytest.raises(ApiError) as caught:
        await runs.lines_ready_to_send(run_id, ["PR-06"])
    assert caught.value.code == "LEASH_ASK_UNRESOLVED"
    asks = (caught.value.details or {})["open_asks"]
    assert [a["line_id"] for a in asks] == [omar.line_id]
    out = runs.get_run_out(run_id)
    assert _line(out, "Omar").owner_action is None and _line(out, "Omar").gross == D("450.00")
    with demo() as db:  # the owner row keeps the approved amount
        row = db.scalars(
            select(lm.LeashDecisionRow).where(lm.LeashDecisionRow.actor == "owner")
        ).one()
        assert row.amount == D("300.00")


@pytest.mark.asyncio
async def test_guard_only_ask_approval_expires_on_amount_jump(
    demo: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = runs.decide

    async def amount_rule(action: Any, facts: dict[str, Any], subject_id: str) -> GuardResult:
        res = await real(action, facts, subject_id)
        if (
            facts.get("member_key") is not None
            and D(facts["amount"]) > D("150.00")
            and res.decision == LeashDecision.ALLOW
        ):
            return res.model_copy(update={"decision": LeashDecision.ASK, "reason": "over 150"})
        return res

    monkeypatch.setattr(runs, "decide", amount_rule)
    run_id = await _prepared(demo)
    out = runs.get_run_out(run_id)
    sara = _line(out, "Sara")
    assert sara.decision == LeashDecision.ASK and sara.flags == []
    for line in out.lines:
        await runs.set_line_action(run_id, line.line_id, "APPROVED", None)
    assert len(await runs.lines_ready_to_send(run_id, ["PR-06"])) == 3

    _add_hours(demo, "Sara", "8.00", 7, "tse_p2")
    _add_hours(demo, "Sara", "8.00", 8, "tse_p3")
    with pytest.raises(ApiError) as caught:
        await runs.lines_ready_to_send(run_id, ["PR-06"])
    assert [a["line_id"] for a in (caught.value.details or {})["open_asks"]] == [sara.line_id]


@pytest.mark.asyncio
async def test_approving_an_unchecked_line_is_refused(demo: SessionMaker) -> None:
    prepared = await asyncio.to_thread(runs._prepare_run, "ms_01")  # no check yet
    assert prepared is not None
    run_id = prepared[0]
    omar = _line(runs.get_run_out(run_id), "Omar")

    with pytest.raises(ApiError) as caught:
        await runs.set_line_action(run_id, omar.line_id, "APPROVED", None)
    assert (caught.value.status_code, caught.value.code) == (409, "LINE_NOT_CHECKED")
    assert _line(runs.get_run_out(run_id), "Omar").owner_action is None

    out = await runs.set_line_action(run_id, omar.line_id, "EXCLUDED", None)
    assert _line(out, "Omar").owner_action == "EXCLUDED"


@pytest.mark.asyncio
async def test_real_interleaving_blocked_explainer(
    demo: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hold the EXCLUDED click's check inside decide() while APPROVED and the gate try to run."""
    run_id = await _prepared(demo)
    omar = _line(runs.get_run_out(run_id), "Omar")
    real = runs.decide
    release = asyncio.Event()
    entered = asyncio.Event()
    armed = {"v": True}

    async def blocking(action: Any, facts: dict[str, Any], subject_id: str) -> GuardResult:
        if armed["v"] and facts.get("member_key") is not None:
            armed["v"] = False
            entered.set()
            await release.wait()
        return await real(action, facts, subject_id)

    monkeypatch.setattr(runs, "decide", blocking)
    first = asyncio.create_task(runs.set_line_action(run_id, omar.line_id, "EXCLUDED", None))
    await entered.wait()
    second = asyncio.create_task(runs.set_line_action(run_id, omar.line_id, "APPROVED", None))
    gate = asyncio.create_task(runs.lines_ready_to_send(run_id, []))
    await asyncio.sleep(0.3)
    progressed = second.done() or gate.done()
    with demo() as db:
        row = db.get(lm.PayrollLineRow, omar.line_id)
        assert row is not None
        stamped = row.owner_action
    release.set()
    done = await asyncio.gather(first, second, gate, return_exceptions=True)

    assert not progressed and stamped == "EXCLUDED"  # APPROVED waited for the lock
    assert isinstance(done[2], ApiError)
    assert (done[2].details or {})["open_run_flags"] == ["PR-06"]
