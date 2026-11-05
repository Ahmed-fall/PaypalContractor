"""Payroll run lifecycle (plan 5.3, A.2.2, A.5), hook H2 and the timesheet/roster flows.

``on_milestone_payment`` is hook H2: after a verified capture it prepares one payroll
run for the milestone (idempotent while a run is unsent) and checks it in the
background. Every decision comes from the deterministic checks in ``checks.py``; they
reach the guard as ``facts["payroll_check"]`` and the guard can only make them
stricter. Money is ``Decimal`` only. Every public async function opens its own
sessions through the module-level ``session_factory`` (tests point it at a temp DB).
Facts sent to ``decide`` carry no email and no name, only the member key.

Concurrency: one ``asyncio.Lock`` per milestone serialises run preparation and one per
run serialises every check, owner action and send-gate read, so a stale check can never
overwrite newer state. The locks are per process (the demo runs a single uvicorn
worker); across processes the partial unique index ``one_open_run_per_milestone`` keeps
a single open run per milestone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ...api.errors import ApiError
from ...db.database import SessionLocal, new_id, utcnow
from ...db.models import (
    Contract,
    Discrepancy,
    Milestone,
    PaypalCapture,
    RemediationInvoice,
)
from ...schemas.contracts import (
    GuardAction,
    LeashDecision,
    MarginResponse,
    MarginRow,
    MarginSummary,
    OwnerAction,
    PayrollLineOut,
    PayrollRunOut,
    TeamMemberCreate,
    TeamMemberOut,
    TeamMemberUpdate,
    TimesheetField,
    TimesheetMappingRequired,
    TimesheetParsed,
)
from ..models import (
    LeashDecisionRow,
    PayrollLineRow,
    PayrollRunRow,
    TeamMemberRow,
    TimesheetEntryRow,
    TimesheetUploadRow,
)
from ..roster import (
    InMemoryRoster,
    RosterConflict,
    TeamMember,
    normalise_name,
    to_out,
)
from ..roster import create_member as _new_member
from ..roster import update_member as _patch_member
from ..service import DecisionRecord, decide
from ..store import (
    SessionFactory,
    SqlDecisionLog,
    SqlMappingStore,
    SqlPolicyProvider,
    SqlRoster,
    aware,
    to_team_member,
)
from .calculate import DraftLine, build_lines, cash_used_pct, run_total
from .checks import LineCheck, MemberHistory, check_line, check_run as run_checks
from .mapping import _load_mapper, confirm_mapping, resolve_mapping
from .parse import RawTable, TimesheetError, file_hash, header_signature, read_table
from .timesheet import TimesheetEntry, parse_entries

logger = logging.getLogger(__name__)

session_factory: SessionFactory = SessionLocal

FINISHED = frozenset({"SENT", "PAID", "PARTIAL", "FAILED"})
_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_ERROR_STATUS = {"FILE_TOO_LARGE": 413, "UNSUPPORTED_FILE_TYPE": 415, "TIMESHEET_INVALID": 422}

_tasks: set[asyncio.Task[Any]] = set()


def _money(value: Decimal) -> str:
    return format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def _not_found(code: str, message: str) -> ApiError:
    return ApiError(404, code, message)


def _not_sendable() -> ApiError:
    return ApiError(409, "RUN_NOT_SENDABLE", "This payroll run has already been sent.")


# --- cash in -------------------------------------------------------------------------


def cash_in_for_milestone(db: Session, milestone_id: str) -> Decimal:
    """Captures in the contract currency plus PAID correction invoices for the milestone."""
    milestone = db.get(Milestone, milestone_id)
    if milestone is None:
        return _ZERO
    contract = db.get(Contract, milestone.contract_id)
    currency = contract.currency if contract is not None else "USD"
    total = _ZERO
    for amount in db.scalars(
        select(PaypalCapture.gross_amount).where(
            PaypalCapture.milestone_id == milestone_id, PaypalCapture.currency == currency
        )
    ):
        total += amount
    for amount in db.scalars(
        select(RemediationInvoice.total_amount)
        .join(Discrepancy, Discrepancy.id == RemediationInvoice.discrepancy_id)
        .where(
            Discrepancy.milestone_id == milestone_id,
            RemediationInvoice.status == "PAID",
            RemediationInvoice.currency == currency,
        )
    ):
        total += amount
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)


def _invoiced_open(db: Session, milestone_id: str) -> Decimal:
    total = _ZERO
    for amount in db.scalars(
        select(RemediationInvoice.total_amount)
        .join(Discrepancy, Discrepancy.id == RemediationInvoice.discrepancy_id)
        .where(Discrepancy.milestone_id == milestone_id, RemediationInvoice.status == "SENT")
    ):
        total += amount
    return total


# --- loading helpers -----------------------------------------------------------------


def _load_entries(db: Session) -> list[TimesheetEntry]:
    rows = db.scalars(
        select(TimesheetEntryRow)
        .join(TimesheetUploadRow, TimesheetUploadRow.id == TimesheetEntryRow.upload_id)
        .where(TimesheetUploadRow.status == "PARSED")
        .order_by(TimesheetEntryRow.work_date, TimesheetEntryRow.id)
    )
    return [
        TimesheetEntry(
            row_number=i,
            member_name=r.raw_member_name,
            member_key=normalise_name(r.raw_member_name),
            work_date=r.work_date,
            hours=r.hours,
            milestone_code=r.milestone_code,
            rate_in_file=r.rate_in_file,
        )
        for i, r in enumerate(rows, start=1)
    ]


def _roster_snapshot() -> InMemoryRoster:
    roster = InMemoryRoster()
    for member in SqlRoster(session_factory).active():
        roster.add(member)
    return roster


# --- H2 ------------------------------------------------------------------------------


_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(kind: str, key: str) -> asyncio.Lock:
    return _locks.setdefault((kind, key), asyncio.Lock())


def _prepare_run(milestone_id: str) -> tuple[str, bool] | None:
    """(run_id, created). None when the milestone is unknown or has no timesheet hours."""
    roster = _roster_snapshot()
    with session_factory() as db:
        milestone = db.get(Milestone, milestone_id)
        if milestone is None:
            logger.warning("leash H2: unknown milestone %s", milestone_id)
            return None
        existing = _open_run(db, milestone_id)
        if existing is not None:
            return existing.id, False
        drafts = build_lines(_load_entries(db), milestone.code, roster)
        if not drafts:
            return None
        cash_in = cash_in_for_milestone(db, milestone_id)
        total = run_total(drafts, set(), set())
        run = PayrollRunRow(
            id=new_id("prn"),
            mode="MILESTONE",
            milestone_id=milestone_id,
            status="PREPARED",
            total=total,
            cash_in_snapshot=cash_in,
            cash_used_pct=cash_used_pct(total, cash_in),
            warnings_json=[
                {"code": "PR-05", "member": d.display_name} for d in drafts if d.member is None
            ],
        )
        try:
            db.add(run)
            db.flush()
            for draft in drafts:
                if draft.member is None or draft.rate is None:
                    continue
                db.add(
                    PayrollLineRow(
                        id=new_id("prl"),
                        run_id=run.id,
                        team_member_id=draft.member.member_id,
                        hours=draft.hours,
                        rate=draft.rate,
                        gross=draft.gross,
                        decision="ASK",  # fail closed until the checks have run
                        flags_json=[],
                    )
                )
            db.commit()
        except IntegrityError:  # another process won the one-open-run index
            db.rollback()
            raced = _open_run(db, milestone_id)
            if raced is None:
                raise
            return raced.id, False
        return run.id, True


def _open_run(db: Session, milestone_id: str) -> PayrollRunRow | None:
    return db.scalars(
        select(PayrollRunRow)
        .where(PayrollRunRow.milestone_id == milestone_id, PayrollRunRow.status.not_in(FINISHED))
        .order_by(PayrollRunRow.created_at, PayrollRunRow.id)
    ).first()


def _task_done(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("leash payroll check failed", exc_info=exc)


async def on_milestone_payment(milestone_id: str) -> str | None:
    """Hook H2: prepare the milestone's payroll run and check it in the background."""
    async with _lock("milestone", milestone_id):
        prepared = await asyncio.to_thread(_prepare_run, milestone_id)
    if prepared is None:
        return None
    run_id, created = prepared
    if not created:
        return run_id
    task = asyncio.get_running_loop().create_task(check_run(run_id))
    _tasks.add(task)
    task.add_done_callback(_task_done)
    return run_id


async def wait_for_background() -> None:
    """Await every pending background check (used by tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# --- checks --------------------------------------------------------------------------


@dataclass
class _LineIn:
    line_id: str
    member_id: str
    member_key: str
    hours: Decimal
    rate: Decimal
    gross: Decimal
    owner_action: str | None
    owner_action_at: datetime | None
    owner_flags: tuple[str, ...]
    email_changed_at: datetime | None


@dataclass
class _Inputs:
    status: str
    code: str
    cash_in: Decimal
    warnings: list[dict[str, Any]]
    approved: bool
    lines: list[_LineIn]
    drafts: dict[str, DraftLine]
    histories: dict[str, MemberHistory]


_COUNTS_AS_PAID = frozenset({"SUCCESS", "PENDING", "UNCLAIMED"})


def _history(db: Session, member_id: str, run_id: str) -> MemberHistory:
    """PR-07 treats a line as paid until it fails: SUCCESS, PENDING, UNCLAIMED or just sent.

    ``last_paid_at`` (PR-01) stays SUCCESS-only.
    """
    rows = db.execute(
        select(PayrollLineRow, PayrollRunRow)
        .join(PayrollRunRow, PayrollRunRow.id == PayrollLineRow.run_id)
        .where(
            PayrollLineRow.team_member_id == member_id,
            PayrollLineRow.run_id != run_id,
            PayrollRunRow.status.in_(("SENT", "PAID", "PARTIAL")),
        )
    ).all()
    last: datetime | None = None
    milestones: set[str] = set()
    for line, run in rows:
        if line.owner_action == "EXCLUDED":
            continue
        status = line.payout_status
        if status is None:  # just sent, unless it was never sendable
            if line.decision == "DENY" or line.gross <= 0:
                continue
        elif status not in _COUNTS_AS_PAID:
            continue  # FAILED: may be paid again
        if status == "SUCCESS":
            paid = aware(run.sent_at) or aware(line.owner_action_at)
            if paid is not None and (last is None or paid > last):
                last = paid
        if run.milestone_id is not None:
            code = db.scalars(select(Milestone.code).where(Milestone.id == run.milestone_id)).first()
            if code is not None:
                milestones.add(code)
    return MemberHistory(last_paid_at=last, paid_milestones=frozenset(milestones))


def _gather_inputs(run_id: str) -> _Inputs:
    roster = _roster_snapshot()
    with session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is None:
            raise _not_found("RUN_NOT_FOUND", "Payroll run not found.")
        code = ""
        if run.milestone_id is not None:
            milestone = db.get(Milestone, run.milestone_id)
            code = milestone.code if milestone is not None else ""
        cash_in = (
            run.cash_in_snapshot
            if run.status in FINISHED or run.milestone_id is None
            else cash_in_for_milestone(db, run.milestone_id)
        )
        drafts = {
            d.member.member_id: d
            for d in build_lines(_load_entries(db), code, roster)
            if d.member is not None
        }
        lines: list[_LineIn] = []
        histories: dict[str, MemberHistory] = {}
        for row in db.scalars(select(PayrollLineRow).where(PayrollLineRow.run_id == run_id)):
            member = db.get(TeamMemberRow, row.team_member_id)
            key = normalise_name(member.name) if member is not None else row.team_member_id
            hours, rate, gross = row.hours, row.rate, row.gross
            draft = drafts.get(row.team_member_id)
            if draft is not None and draft.rate is not None and run.status not in FINISHED:
                # Until it is sent the line follows the current timesheet and roster rate.
                hours, rate, gross = draft.hours, draft.rate, draft.gross
            lines.append(
                _LineIn(
                    row.id,
                    row.team_member_id,
                    key,
                    hours,
                    rate,
                    gross,
                    row.owner_action,
                    aware(row.owner_action_at),
                    tuple(row.owner_flags_json or ()),
                    aware(member.email_changed_at) if member is not None else None,
                )
            )
            histories[row.team_member_id] = _history(db, row.team_member_id, run_id)
        return _Inputs(
            status=run.status,
            code=code,
            cash_in=cash_in,
            warnings=list(run.warnings_json or []),
            approved=run.approved_at is not None,
            lines=lines,
            drafts=drafts,
            histories=histories,
        )


def _persist_check(
    run_id: str,
    lines: list[_LineIn],
    outcome: dict[str, tuple[LeashDecision, str, tuple[str, ...], str | None]],
    run_decision_id: str,
    total: Decimal,
    cash_in: Decimal,
    status: str,
    stale_approvals: frozenset[str] = frozenset(),
) -> None:
    with session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is None:
            raise _not_found("RUN_NOT_FOUND", "Payroll run not found.")
        for line in lines:
            row = db.get(PayrollLineRow, line.line_id)
            if row is None:
                continue
            decision, decision_id, flags, reason = outcome[line.line_id]
            row.hours, row.rate, row.gross = line.hours, line.rate, line.gross
            row.decision = decision.value
            row.decision_id = decision_id
            row.flags_json = list(flags)
            row.reason = reason
            if line.line_id in stale_approvals:
                row.owner_action = None
                row.owner_action_at = None
                row.owner_flags_json = None
        run.run_decision_id = run_decision_id
        run.total = total
        run.cash_in_snapshot = cash_in
        run.cash_used_pct = cash_used_pct(total, cash_in)
        run.status = status
        db.commit()


def _approval_covers(line: _LineIn, flags: tuple[str, ...]) -> bool:
    """An APPROVED action holds only for the risk the owner saw when approving."""
    if line.owner_action_at is None or not set(flags) <= set(line.owner_flags):
        return False
    return line.email_changed_at is None or line.email_changed_at <= line.owner_action_at


async def check_run(run_id: str) -> PayrollRunOut:
    """Run all 7 checks over a run, log the decisions and set its status."""
    async with _lock("run", run_id):
        return await _check_run_locked(run_id)


async def _check_run_locked(run_id: str) -> PayrollRunOut:
    inputs = await asyncio.to_thread(_gather_inputs, run_id)
    if inputs.status in FINISHED:
        return await asyncio.to_thread(get_run_out, run_id)
    policy = await SqlPolicyProvider(session_factory).active_policy()
    now = datetime.now(UTC)

    checks: dict[str, LineCheck] = {}
    for line in inputs.lines:
        draft = inputs.drafts.get(line.member_id)
        if draft is None:  # no hours for this member any more: never pay it
            checks[line.line_id] = LineCheck(line.member_key, LeashDecision.DENY, ("PR-05",))
            continue
        checks[line.line_id] = check_line(
            draft, inputs.histories[line.member_id], inputs.code, policy, now
        )

    line_results = await asyncio.gather(
        *[
            decide(
                GuardAction.PAY_TEAM_MEMBER,
                {
                    "payroll_check": {
                        "decision": checks[ln.line_id].decision.value,
                        "flags": list(checks[ln.line_id].flags),
                    },
                    "amount": _money(ln.gross),
                    "hours": _money(ln.hours),
                    "member_key": ln.member_key,
                },
                ln.line_id,
            )
            for ln in inputs.lines
        ]
    )

    stale: set[str] = set()
    for ln in inputs.lines:
        if ln.owner_action == "APPROVED" and not _approval_covers(ln, checks[ln.line_id].flags):
            stale.add(ln.line_id)
            ln.owner_action = None  # new risk since the approval: the line is open again
    excluded = {ln.member_key for ln in inputs.lines if ln.owner_action == "EXCLUDED"}
    denied = {
        ln.member_key
        for ln, res in zip(inputs.lines, line_results, strict=True)
        if res.decision == LeashDecision.DENY
    }
    priced = [
        replace(inputs.drafts[ln.member_id], gross=ln.gross, hours=ln.hours, rate=ln.rate)
        for ln in inputs.lines
        if ln.member_id in inputs.drafts
    ]
    # Lines with no draft are denied above and carry no weight in the total.
    total = run_total(priced, excluded=excluded, denied=denied)
    pct = cash_used_pct(total, inputs.cash_in)

    run_check = run_checks(total, inputs.cash_in, policy)
    run_flags = set(run_check.flags)
    if any(w.get("code") == "PR-05" for w in inputs.warnings):
        run_flags.add("PR-05")
    ordered_flags = sorted(run_flags)
    run_decision = LeashDecision.ASK if ordered_flags else LeashDecision.ALLOW
    run_result = await decide(
        GuardAction.SEND_PAYROLL_BATCH,
        {
            "payroll_check": {"decision": run_decision.value, "flags": ordered_flags},
            "amount": _money(total),
            "cash_in": _money(inputs.cash_in),
            "cash_used_pct": _money(pct),
        },
        run_id,
    )

    outcome: dict[str, tuple[LeashDecision, str, tuple[str, ...], str | None]] = {}
    open_ask = False
    for ln, result in zip(inputs.lines, line_results, strict=True):
        reason = None if result.decision == LeashDecision.ALLOW else result.reason
        outcome[ln.line_id] = (
            result.decision,
            result.decision_id,
            checks[ln.line_id].flags,
            reason,
        )
        if result.decision == LeashDecision.ASK and ln.owner_action is None:
            open_ask = True
    run_open = bool(ordered_flags) and not inputs.approved
    status = "AWAITING_APPROVAL" if open_ask or run_open else "CHECKED"
    await asyncio.to_thread(
        _persist_check,
        run_id,
        inputs.lines,
        outcome,
        run_result.decision_id,
        total,
        inputs.cash_in,
        status,
        frozenset(stale),
    )
    return await asyncio.to_thread(get_run_out, run_id)


# --- output --------------------------------------------------------------------------


def _run_out(db: Session, run: PayrollRunRow) -> PayrollRunOut:
    decision = LeashDecision.ASK  # unchecked runs fail closed
    flags: list[str] = []
    if run.run_decision_id is not None:
        drow = db.get(LeashDecisionRow, run.run_decision_id)
        if drow is not None:
            decision = LeashDecision(drow.decision)
            check = drow.facts_json.get("payroll_check", {})
            flags = [str(f) for f in check.get("flags", [])]
    lines: list[PayrollLineOut] = []
    members: dict[str, TeamMemberRow] = {}
    for row in db.scalars(select(PayrollLineRow).where(PayrollLineRow.run_id == run.id)):
        member = db.get(TeamMemberRow, row.team_member_id)
        if member is None:
            continue
        members[row.id] = member
        lines.append(
            PayrollLineOut(
                line_id=row.id,
                member_name=member.name,
                payout_email=member.payout_email,
                hours=row.hours,
                rate=row.rate,
                gross=row.gross,
                decision=LeashDecision(row.decision),
                flags=list(row.flags_json or []),
                reason=row.reason,
                owner_action=row.owner_action,  # type: ignore[arg-type]
                payout_status=row.payout_status,  # type: ignore[arg-type]
            )
        )
    lines.sort(key=lambda ln: (ln.member_name.casefold(), ln.line_id))
    return PayrollRunOut(
        run_id=run.id,
        mode=run.mode,  # type: ignore[arg-type]
        milestone_id=run.milestone_id,
        period=run.period,
        status=run.status,  # type: ignore[arg-type]
        total=run.total,
        cash_in=run.cash_in_snapshot,
        cash_used_pct=run.cash_used_pct,
        run_decision=decision,
        run_flags=flags,
        lines=lines,
        sender_batch_id=run.sender_batch_id,
    )


def get_run_out(run_id: str) -> PayrollRunOut:
    with session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is None:
            raise _not_found("RUN_NOT_FOUND", "Payroll run not found.")
        return _run_out(db, run)


# --- owner actions and the send gate ---------------------------------------------------


def _validated_line(db: Session, run_id: str, line_id: str) -> tuple[PayrollRunRow, PayrollLineRow]:
    run = db.get(PayrollRunRow, run_id)
    if run is None:
        raise _not_found("RUN_NOT_FOUND", "Payroll run not found.")
    if run.status in FINISHED:
        raise _not_sendable()
    line = db.get(PayrollLineRow, line_id)
    if line is None or line.run_id != run_id:
        raise _not_found("LINE_NOT_FOUND", "Payroll line not found.")
    return run, line


def _stamp_owner_action(
    run_id: str, line_id: str, owner_action: OwnerAction, now: datetime
) -> Decimal:
    """Validate, then stamp the owner action; returns the line's gross."""
    with session_factory() as db:
        run, line = _validated_line(db, run_id, line_id)
        line.owner_action = owner_action
        line.owner_action_at = now
        line.owner_flags_json = list(line.flags_json or [])
        run.approved_at = None  # any change re-opens the run flags
        db.commit()
        return line.gross


async def set_line_action(
    run_id: str, line_id: str, owner_action: OwnerAction, note: str | None
) -> PayrollRunOut:
    async with _lock("run", run_id):
        now = datetime.now(UTC)
        policy = await SqlPolicyProvider(session_factory).active_policy()
        gross = await asyncio.to_thread(_stamp_owner_action, run_id, line_id, owner_action, now)
        approved = owner_action == "APPROVED"
        facts: dict[str, Any] = {"owner_action": owner_action}
        if note:
            facts["note"] = note
        await SqlDecisionLog(session_factory).append(
            DecisionRecord(
                decision_id=new_id("lsd"),
                policy_version=policy.version,
                action_type=GuardAction.PAY_TEAM_MEMBER,
                subject_type="payroll_line",
                subject_id=line_id,
                decision=LeashDecision.ALLOW if approved else LeashDecision.DENY,
                rule_ids=("OWNER",),
                facts=facts,
                amount=gross,
                reason="Owner approved this line." if approved else "Owner excluded this line.",
                actor="owner",
                created_at=now,
            )
        )
        return await _check_run_locked(run_id)


def _approve_flags(run_id: str, now: datetime) -> None:
    with session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is not None and run.approved_at is None:
            run.approved_at = now
            db.commit()


async def lines_ready_to_send(run_id: str, approve_run_flags: list[str]) -> list[PayrollLineOut]:
    """Re-check the run, then return the lines to pay or raise 409 (A.2.2)."""
    async with _lock("run", run_id):
        return await _lines_ready_locked(run_id, approve_run_flags)


async def _lines_ready_locked(run_id: str, approve_run_flags: list[str]) -> list[PayrollLineOut]:
    current = await asyncio.to_thread(get_run_out, run_id)
    if current.status in FINISHED:
        raise _not_sendable()
    out = await _check_run_locked(run_id)
    if out.status in FINISHED:
        raise _not_sendable()
    if out.run_decision == LeashDecision.DENY:
        raise ApiError(409, "LEASH_DENIED", "Leash has denied this payroll run.")
    open_asks = [
        {"line_id": ln.line_id, "flags": ln.flags}
        for ln in out.lines
        if ln.decision == LeashDecision.ASK and ln.owner_action is None
    ]
    open_flags = [f for f in out.run_flags if f not in approve_run_flags]
    if open_asks or open_flags:
        count = len(open_asks)
        message = (
            f"{count} line(s) still need your decision before the team can be paid."
            if count
            else "This run's flags still need your approval before the team can be paid."
        )
        raise ApiError(
            409,
            "LEASH_ASK_UNRESOLVED",
            message,
            {"open_asks": open_asks, "open_run_flags": open_flags},
        )
    if out.run_flags:
        await asyncio.to_thread(_approve_flags, run_id, datetime.now(UTC))
    return [
        ln
        for ln in out.lines
        if ln.owner_action != "EXCLUDED"
        and ln.gross > 0
        and (
            ln.decision == LeashDecision.ALLOW
            or (ln.decision == LeashDecision.ASK and ln.owner_action == "APPROVED")
        )
    ]


def recompute_run_status(run_id: str) -> PayrollRunOut:
    """Roll item statuses up to the run (A.5). Only for runs that are SENT or later."""
    with session_factory() as db:
        run = db.get(PayrollRunRow, run_id)
        if run is None:
            raise _not_found("RUN_NOT_FOUND", "Payroll run not found.")
        if run.status in FINISHED:
            sent = [
                ln
                for ln in db.scalars(select(PayrollLineRow).where(PayrollLineRow.run_id == run_id))
                if ln.payout_status is not None
            ]
            if run.batch_status == "DENIED":
                for ln in sent:
                    ln.payout_status = "FAILED"
                run.status = "FAILED"
            elif sent:
                statuses = {ln.payout_status for ln in sent}
                if statuses == {"SUCCESS"}:
                    run.status = "PAID"
                    run.completed_at = run.completed_at or utcnow()
                elif "PENDING" not in statuses:
                    run.status = "PARTIAL"
            db.commit()
        return _run_out(db, run)


# --- margin ----------------------------------------------------------------------------


def margin_rows(db: Session) -> MarginResponse:
    """One row per milestone of an ACTIVE contract (REQ-LSH-14)."""
    milestones = db.scalars(
        select(Milestone)
        .join(Contract, Contract.id == Milestone.contract_id)
        .where(Contract.status == "ACTIVE")
        .order_by(Milestone.contract_id, Milestone.code)
    ).all()
    rows: list[MarginRow] = []
    sum_cash = sum_paid = _ZERO
    for ms in milestones:
        cash_in = cash_in_for_milestone(db, ms.id)
        paid = pending = _ZERO
        latest: PayrollRunRow | None = None
        runs = db.scalars(
            select(PayrollRunRow)
            .where(PayrollRunRow.milestone_id == ms.id)
            .order_by(PayrollRunRow.created_at.desc(), PayrollRunRow.id.desc())
        ).all()
        if runs:
            latest = runs[0]
        for run in runs:
            for ln in db.scalars(select(PayrollLineRow).where(PayrollLineRow.run_id == run.id)):
                if ln.payout_status == "SUCCESS":
                    paid += ln.gross
                elif ln.payout_status in ("PENDING", "UNCLAIMED"):
                    pending += ln.gross
        margin = cash_in - paid
        pct = (
            (margin / cash_in * 100).quantize(_CENT, rounding=ROUND_HALF_UP)
            if cash_in > 0
            else _ZERO
        )
        rows.append(
            MarginRow(
                milestone_id=ms.id,
                milestone_code=ms.code,
                cash_in=cash_in,
                invoiced_open=_invoiced_open(db, ms.id),
                paid_to_team=paid,
                pending_to_team=pending,
                margin=margin,
                margin_pct=pct,
                payroll_status=latest.status if latest is not None else "NONE",  # type: ignore[arg-type]
            )
        )
        sum_cash += cash_in
        sum_paid += paid
    return MarginResponse(
        rows=rows,
        summary=MarginSummary(cash_in=sum_cash, paid_to_team=sum_paid, margin=sum_cash - sum_paid),
    )


# --- timesheet and roster services ------------------------------------------------------


def _factory_for(db: Session) -> Callable[[], Session]:
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False, future=True)


def _timesheet_api_error(exc: TimesheetError) -> ApiError:
    return ApiError(_ERROR_STATUS.get(exc.code, 422), exc.code, exc.message, exc.details)


def _json_cell(value: object) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return value
    return str(value)


def _store_entries(
    db: Session, upload: TimesheetUploadRow, entries: tuple[TimesheetEntry, ...]
) -> TimesheetParsed:
    by_key: dict[str, str | None] = {}
    for row in db.scalars(select(TeamMemberRow).where(TeamMemberRow.active.is_(True))):
        key = normalise_name(row.name)
        by_key[key] = None if key in by_key else row.id  # ambiguous names match nobody
    unknown: set[str] = set()
    for e in entries:
        member_id = by_key.get(e.member_key)
        if member_id is None:
            unknown.add(e.member_name)
        db.add(
            TimesheetEntryRow(
                id=new_id("tse"),
                upload_id=upload.id,
                team_member_id=member_id,
                raw_member_name=e.member_name,
                work_date=e.work_date,
                hours=e.hours,
                milestone_code=e.milestone_code,
                rate_in_file=e.rate_in_file,
            )
        )
    upload.status = "PARSED"
    upload.table_json = None
    db.commit()
    return TimesheetParsed(
        upload_id=upload.id,
        status="PARSED",
        entries=len(entries),
        unknown_members=sorted(unknown),
        milestones=sorted({e.milestone_code for e in entries}),
        warnings=[],
    )


async def upload_timesheet(
    db: Session, data: bytes, filename: str
) -> TimesheetParsed | TimesheetMappingRequired:
    digest = file_hash(data)
    existing = db.scalars(
        select(TimesheetUploadRow).where(TimesheetUploadRow.file_hash == digest)
    ).first()
    if existing is not None:
        raise ApiError(
            409,
            "DUPLICATE_UPLOAD",
            "This timesheet was already uploaded.",
            {"upload_id": existing.id},
        )
    try:
        table = read_table(data, filename)
        store = SqlMappingStore(_factory_for(db))
        resolution = await resolve_mapping(table, store, _load_mapper())
        entries = parse_entries(table, resolution.mapping) if resolution.mapping is not None else None
    except TimesheetError as exc:
        raise _timesheet_api_error(exc) from exc

    upload = TimesheetUploadRow(
        id=new_id("tsu"),
        file_name=filename[:255],
        file_hash=digest,
        header_signature=resolution.signature,
        row_count=len(table.rows),
        status="PARSED" if entries is not None else "MAPPING_REQUIRED",
    )
    if entries is not None:
        db.add(upload)
        db.flush()
        return _store_entries(db, upload, entries)
    assert resolution.proposal is not None
    upload.table_json = {
        "headers": list(table.headers),
        "rows": [[_json_cell(c) for c in row] for row in table.rows],
        "row_numbers": list(table.row_numbers),
    }
    db.add(upload)
    db.commit()
    return TimesheetMappingRequired(
        upload_id=upload.id,
        status="MAPPING_REQUIRED",
        row_count=upload.row_count,
        headers=list(table.headers),
        proposal=resolution.proposal,
    )


async def confirm_timesheet_mapping(
    db: Session, upload_id: str, mapping: dict[str, TimesheetField]
) -> TimesheetParsed:
    upload = db.get(TimesheetUploadRow, upload_id)
    if upload is None:
        raise _not_found("UPLOAD_NOT_FOUND", "Timesheet upload not found.")
    if upload.status != "MAPPING_REQUIRED" or not isinstance(upload.table_json, dict):
        raise ApiError(409, "UPLOAD_NOT_PENDING", "This timesheet does not need a column mapping.")
    stored = upload.table_json
    table = RawTable(
        headers=tuple(stored["headers"]),
        rows=tuple(tuple(r) for r in stored["rows"]),
        row_numbers=tuple(stored["row_numbers"]),
    )
    try:
        confirmed = await confirm_mapping(
            SqlMappingStore(_factory_for(db)),
            header_signature(table.headers),
            dict(mapping),
            table.headers,
        )
        entries = parse_entries(table, confirmed)
    except TimesheetError as exc:
        raise _timesheet_api_error(exc) from exc
    return _store_entries(db, upload, entries)


def _conflict(exc: RosterConflict) -> ApiError:
    code = "MEMBER_NAME_IN_USE" if exc.kind == "name" else "PAYOUT_EMAIL_IN_USE"
    text = str(exc)
    return ApiError(409, code, text[:1].upper() + text[1:] + ".")


def create_member(db: Session, data: TeamMemberCreate) -> TeamMemberOut:
    member = _new_member(data, utcnow())
    try:
        SqlRoster(_factory_for(db)).add(member)
    except RosterConflict as exc:
        raise _conflict(exc) from exc
    return to_out(member)


def update_member(db: Session, member_id: str, patch: TeamMemberUpdate) -> TeamMemberOut:
    roster = SqlRoster(_factory_for(db))
    current = roster.get(member_id)
    if current is None:
        raise _not_found("TEAM_MEMBER_NOT_FOUND", "Team member not found.")
    updated: TeamMember = _patch_member(current, patch, utcnow())
    try:
        roster.replace(updated)
    except RosterConflict as exc:
        raise _conflict(exc) from exc
    return to_out(updated)


def list_members(db: Session) -> list[TeamMemberOut]:
    rows = db.scalars(select(TeamMemberRow).order_by(TeamMemberRow.created_at, TeamMemberRow.id))
    return [to_out(to_team_member(r)) for r in rows]


__all__ = [
    "FINISHED",
    "cash_in_for_milestone",
    "check_run",
    "confirm_timesheet_mapping",
    "create_member",
    "get_run_out",
    "lines_ready_to_send",
    "list_members",
    "margin_rows",
    "on_milestone_payment",
    "recompute_run_status",
    "set_line_action",
    "update_member",
    "upload_timesheet",
    "wait_for_background",
]
