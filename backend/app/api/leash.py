"""Leash REST API (plan A.2.2): policy, decisions, team, timesheets, payroll runs, margin.

Mounted by ``main.py`` (``importlib "app.api.leash"``, attribute ``router``). Importing
this module imports ``..leash.models`` so ``init_db``'s ``create_all`` creates the Leash
tables. Router-level dependencies run once per process: ``_ensure_tables`` (backup
``create_all``), ``_seed_once`` (demo seed when ``SEED_DEMO_DATA`` and the roster is
empty) and ``_resume_once`` (restart Payouts polling after a restart or sleep, A.1).

Every write needs ``X-Demo-Key`` and passes the write rate limit. Money is a 2-dp string
via the contract models. The routes only wire: decisions come from ``leash.decide`` and
the payroll checks, the batch from ``send_payroll_batch``; no AI output computes an
amount or turns an Ask or Deny into an Allow.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from ..db.database import Base, get_db, new_id, utcnow
from ..db.models import Milestone
from ..leash import models as leash_models  # registers the tables for init_db
from ..leash.models import (
    LEASH_TABLES,
    LeashDecisionRow,
    LeashPolicyRow,
    PayrollRunRow,
    TeamMemberRow,
    TimesheetUploadRow,
)
from ..leash.payroll import runs
from ..leash.policy import BUILTIN_RULES
from ..leash.seed import seed_leash_demo
from ..leash.store import SqlPolicyProvider, aware
from ..schemas.contracts import (
    ActiveLeashPolicy,
    DecisionLogEntry,
    DecisionLogResponse,
    DecisionSubjectType,
    GuardAction,
    LeashDecision,
    LeashPolicy,
    LeashRule,
    MarginResponse,
    PayrollLineUpdate,
    PayrollRunCreate,
    PayrollRunOut,
    PayrollSendRequest,
    PayrollSendResponse,
    PolicyActivateRequest,
    PolicyActivateResponse,
    PolicyCompileRequest,
    PolicyCompileResponse,
    TeamMemberCreate,
    TeamMemberOut,
    TeamMemberUpdate,
    TimesheetMappingConfirm,
    TimesheetMappingRequired,
    TimesheetParsed,
)
from ..services.paypal import payouts, payouts_sync
from .deps import limit_writes, require_demo_key
from .errors import ApiError

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
RECENT_RUNS = 20
DRAFT_SOURCE_TEXT = "(plain form)"
_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")

# --- once-per-process startup hooks ----------------------------------------------------

_startup_lock = asyncio.Lock()
_tables_ready = False
_seeded = False
_resumed = False


def _factory(db: Session) -> Callable[[], Session]:
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False, future=True)


def _create_tables(db: Session) -> None:
    Base.metadata.create_all(db.get_bind(), tables=LEASH_TABLES, checkfirst=True)


def _seed_if_empty(db: Session) -> None:
    if db.scalars(select(TeamMemberRow.id).limit(1)).first() is None:
        seed_leash_demo(db, now=utcnow())


async def _ensure_tables(db: Session = Depends(get_db)) -> None:
    global _tables_ready
    if _tables_ready:
        return
    async with _startup_lock:
        if not _tables_ready:
            await asyncio.to_thread(_create_tables, db)
            _tables_ready = True


async def _seed_once(db: Session = Depends(get_db)) -> None:
    global _seeded
    if _seeded:
        return
    async with _startup_lock:
        if _seeded:
            return
        if get_settings().seed_demo_data:
            await asyncio.to_thread(_seed_if_empty, db)
        _seeded = True


async def _resume_once() -> None:
    global _resumed
    if _resumed:
        return
    async with _startup_lock:
        if _resumed:
            return
        _resumed = True
        try:
            await payouts_sync.resume_pending()
        except Exception:
            logger.exception("leash: resuming payout polling failed")


router = APIRouter(
    prefix="/api/v1/leash",
    tags=["leash"],
    dependencies=[Depends(_ensure_tables), Depends(_seed_once), Depends(_resume_once)],
)
_write = [Depends(require_demo_key), Depends(limit_writes)]


@contextmanager
def _canonical_404() -> Iterator[None]:
    """Unknown ids are always ``NOT_FOUND`` (A.4), whatever the service called them."""
    try:
        yield
    except ApiError as exc:
        if exc.status_code == 404 and exc.code != "NOT_FOUND":
            raise ApiError(404, "NOT_FOUND", exc.message) from exc
        raise


# --- policy ------------------------------------------------------------------------------

_SUBJECT = {
    GuardAction.SEND_REMEDIATION_INVOICE: "the correction invoice",
    GuardAction.PAY_TEAM_MEMBER: "the payment",
    GuardAction.SEND_PAYROLL_BATCH: "the payroll run",
}
_OUTCOME = {
    LeashDecision.ALLOW: "Leash goes ahead on its own",
    LeashDecision.ASK: "Leash waits for your approval",
    LeashDecision.DENY: "Leash blocks {subject}",
}


def _money_text(value: object) -> str:
    try:
        return f"${Decimal(str(value)).quantize(_CENT)}"
    except ArithmeticError:
        return str(value)


def _condition_text(key: str, value: object) -> str:
    if key == "amount_lte":
        return f"the amount is {_money_text(value)} or less"
    if key == "amount_gt":
        return f"the amount is above {_money_text(value)}"
    if key == "confidence_gte":
        return f"the contract reading is at least {value} confident"
    if key == "clause_quote_present":
        return "the breached clause is quoted" if value else "the breached clause is not quoted"
    if key == "client_tag":
        return f"the client is tagged {value}"
    if key == "invoiced_same_client_within_days":
        return f"the same client was invoiced in the last {value} days"
    if key == "line_items_sum_mismatch":
        return "the line items do not add up to the total"
    return f"{key.replace('_', ' ')} is {value}"


def _rule_card(rule: LeashRule) -> str:
    conditions = " and ".join(_condition_text(k, v) for k, v in rule.when.items())
    subject = _SUBJECT[rule.applies_to]
    outcome = _OUTCOME[rule.outcome].format(subject=subject)
    prefix = f"When {conditions}" if conditions else "Always"
    return f"{prefix}, {outcome} ({rule.id})."


def policy_cards(policy: LeashPolicy) -> list[str]:
    """One plain sentence per rule plus the two payroll limits; written in code, no AI."""
    cards = [_rule_card(rule) for rule in policy.rules]
    cards.append(
        f"Payroll: a changed payout email (last {policy.email_change_window_days} days) "
        "waits for you."
    )
    cards.append(f"Payroll: a run above {policy.payroll_margin_limit_pct}% of the money "
                 "received waits for you.")
    return cards


def _with_builtins(policy: LeashPolicy, version: int) -> LeashPolicy:
    present = {rule.id for rule in policy.rules}
    rules = [*policy.rules, *(r for r in BUILTIN_RULES if r.id not in present)]
    return policy.model_copy(update={"version": version, "rules": rules})


def _create_draft(db: Session, policy: LeashPolicy, source_text: str) -> PolicyCompileResponse:
    latest = db.scalars(select(LeashPolicyRow.version).order_by(LeashPolicyRow.version.desc())).first()
    version = (latest or 0) + 1
    draft = _with_builtins(policy, version)
    db.add(
        LeashPolicyRow(
            id=new_id("lpol"),
            version=version,
            source_text=source_text,
            compiled_json=draft.model_dump(mode="json"),
            active=False,
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "POLICY_VERSION_CONFLICT", "Another draft was just saved. Retry.") from exc
    return PolicyCompileResponse(
        draft_version=version,
        status="DRAFT",
        policy=draft,
        cards=policy_cards(draft),
        test_cases=[],
    )


def _load_compiler() -> Callable[[str], Any] | None:
    """P2's ``compile_leash_policy`` if ``..ai.leash`` provides it, else None."""
    try:
        name = f"{__name__.rsplit('.', 2)[0]}.ai.leash"
        if importlib.util.find_spec(name) is None:
            return None
        fn = getattr(importlib.import_module(name), "compile_leash_policy", None)
        return fn if callable(fn) else None
    except Exception:  # noqa: BLE001 - a broken optional module means no compiler
        return None


@router.get("/policy", response_model=ActiveLeashPolicy)
async def get_policy(db: Session = Depends(get_db)) -> ActiveLeashPolicy:
    policy = await SqlPolicyProvider(_factory(db)).active_policy()
    row = db.scalars(select(LeashPolicyRow).where(LeashPolicyRow.active.is_(True))).first()
    activated = aware(row.activated_at or row.created_at) if row is not None else utcnow()
    return ActiveLeashPolicy.model_validate(
        {**policy.model_dump(mode="json"), "activated_at": activated}
    )


@router.post("/policy", response_model=PolicyCompileResponse, dependencies=_write)
def create_policy_draft(body: LeashPolicy, db: Session = Depends(get_db)) -> PolicyCompileResponse:
    """Plain-form fallback (Fallback E 1): save the submitted rules as a draft."""
    return _create_draft(db, body, DRAFT_SOURCE_TEXT)


@router.post("/policy/compile", response_model=PolicyCompileResponse, dependencies=_write)
async def compile_policy(
    body: PolicyCompileRequest, db: Session = Depends(get_db)
) -> PolicyCompileResponse:
    compiler = _load_compiler()
    if compiler is None:
        raise ApiError(501, "COMPILER_UNAVAILABLE", "Use the plain policy form.")
    try:
        result = compiler(body.text)
        if inspect.isawaitable(result):
            result = await result
        policy = (
            result if isinstance(result, LeashPolicy) else LeashPolicy.model_validate(result)
        )
    except Exception as exc:
        logger.warning("leash: policy compiler failed (%s)", type(exc).__name__)
        raise ApiError(
            422, "POLICY_COMPILE_FAILED", "The policy could not be compiled. Use the plain form."
        ) from exc
    return _create_draft(db, policy, body.text)


def _activate(db: Session, version: int) -> PolicyActivateResponse:
    row = db.scalars(select(LeashPolicyRow).where(LeashPolicyRow.version == version)).first()
    if row is None:
        raise ApiError(409, "POLICY_DRAFT_NOT_FOUND", "That policy version was never saved.")
    if not row.active:
        # One transaction; the partial unique index allows a single active row.
        db.execute(update(LeashPolicyRow).where(LeashPolicyRow.active.is_(True)).values(active=False))
        row.active = True
        row.activated_at = utcnow()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ApiError(409, "POLICY_VERSION_CONFLICT", "Another policy was just activated.") from exc
    return PolicyActivateResponse(
        version=row.version, active=True, activated_at=aware(row.activated_at or utcnow())
    )


@router.post("/policy/activate", response_model=PolicyActivateResponse, dependencies=_write)
def activate_policy(
    body: PolicyActivateRequest, db: Session = Depends(get_db)
) -> PolicyActivateResponse:
    return _activate(db, body.version)


# --- decisions ---------------------------------------------------------------------------


def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    created, sep, decision_id = cursor.partition("|")
    try:
        if not sep or not decision_id:
            raise ValueError(cursor)
        return datetime.fromisoformat(created), decision_id
    except ValueError as exc:
        raise ApiError(
            422, "VALIDATION_ERROR", "cursor is not valid", {"fields": [{"field": "cursor"}]}
        ) from exc


@router.get("/decisions", response_model=DecisionLogResponse)
def list_decisions(
    subject_type: DecisionSubjectType | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    db: Session = Depends(get_db),
) -> DecisionLogResponse:
    stmt = select(LeashDecisionRow)
    if subject_type is not None:
        stmt = stmt.where(LeashDecisionRow.subject_type == subject_type)
    if cursor is not None:
        created, decision_id = _parse_cursor(cursor)
        stmt = stmt.where(
            or_(
                LeashDecisionRow.created_at < created,
                and_(LeashDecisionRow.created_at == created, LeashDecisionRow.id < decision_id),
            )
        )
    rows = list(
        db.scalars(
            stmt.order_by(LeashDecisionRow.created_at.desc(), LeashDecisionRow.id.desc()).limit(
                limit + 1
            )
        )
    )
    page = rows[:limit]
    next_cursor = (
        f"{aware(page[-1].created_at).isoformat()}|{page[-1].id}" if len(rows) > limit else None
    )
    items = [
        DecisionLogEntry(
            decision_id=r.id,
            created_at=aware(r.created_at),
            policy_version=r.policy_version,
            action_type=GuardAction(r.action_type),
            subject_type=r.subject_type,  # type: ignore[arg-type]
            subject_id=r.subject_id,
            decision=LeashDecision(r.decision),
            rule_ids=list(r.rule_ids_json),
            amount=r.amount,
            reason=r.reason,
            actor=r.actor,  # type: ignore[arg-type]
        )
        for r in page
    ]
    return DecisionLogResponse(items=items, next_cursor=next_cursor)


# --- team --------------------------------------------------------------------------------


@router.get("/team", response_model=list[TeamMemberOut])
def list_team(db: Session = Depends(get_db)) -> list[TeamMemberOut]:
    return runs.list_members(db)


@router.post("/team", response_model=TeamMemberOut, status_code=201, dependencies=_write)
def create_team_member(body: TeamMemberCreate, db: Session = Depends(get_db)) -> TeamMemberOut:
    return runs.create_member(db, body)


@router.patch("/team/{member_id}", response_model=TeamMemberOut, dependencies=_write)
def update_team_member(
    member_id: str, body: TeamMemberUpdate, db: Session = Depends(get_db)
) -> TeamMemberOut:
    with _canonical_404():
        return runs.update_member(db, member_id, body)


# --- timesheets --------------------------------------------------------------------------


@router.post(
    "/timesheets",
    response_model=TimesheetMappingRequired | TimesheetParsed,
    status_code=201,
    dependencies=_write,
)
async def upload_timesheet(
    file: UploadFile = File(...), db: Session = Depends(get_db)
) -> TimesheetMappingRequired | TimesheetParsed:
    # One byte over the limit is enough for the reader to reject the file.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    return await runs.upload_timesheet(db, data, file.filename or "upload")


@router.post(
    "/timesheets/{upload_id}/mapping", response_model=TimesheetParsed, dependencies=_write
)
async def confirm_timesheet_mapping(
    upload_id: str, body: TimesheetMappingConfirm, db: Session = Depends(get_db)
) -> TimesheetParsed:
    with _canonical_404():
        return await runs.confirm_timesheet_mapping(db, upload_id, dict(body.mapping))


# --- payroll runs ------------------------------------------------------------------------


@router.post("/payroll/runs", response_model=PayrollRunOut, dependencies=_write)
async def create_payroll_run(
    body: PayrollRunCreate, db: Session = Depends(get_db)
) -> PayrollRunOut:
    if body.mode == "MONTHLY":
        raise ApiError(501, "MONTHLY_NOT_AVAILABLE", "Monthly payroll is not available yet.")
    assert body.milestone_id is not None  # PayrollRunCreate guarantees it for MILESTONE
    if db.get(Milestone, body.milestone_id) is None:
        raise ApiError(404, "NOT_FOUND", "Milestone not found.")
    pending = db.scalars(
        select(TimesheetUploadRow).where(TimesheetUploadRow.status == "MAPPING_REQUIRED")
    ).first()
    if pending is not None:
        raise ApiError(
            409,
            "MAPPING_REQUIRED",
            "A timesheet still needs its column mapping.",
            {"upload_id": pending.id},
        )
    run_id = await runs.on_milestone_payment(body.milestone_id)
    if run_id is None:
        raise ApiError(404, "NOT_FOUND", "No timesheet hours for this milestone.")
    await runs.check_run(run_id)
    return runs.get_run_out(run_id)


@router.get("/payroll/runs", response_model=list[PayrollRunOut])
def list_payroll_runs(db: Session = Depends(get_db)) -> list[PayrollRunOut]:
    ids = db.scalars(
        select(PayrollRunRow.id)
        .order_by(PayrollRunRow.created_at.desc(), PayrollRunRow.id.desc())
        .limit(RECENT_RUNS)
    ).all()
    return [runs.get_run_out(run_id) for run_id in ids]


@router.get("/payroll/runs/{run_id}", response_model=PayrollRunOut)
def get_payroll_run(run_id: str) -> PayrollRunOut:
    with _canonical_404():
        return runs.get_run_out(run_id)


@router.patch(
    "/payroll/runs/{run_id}/lines/{line_id}", response_model=PayrollRunOut, dependencies=_write
)
async def update_payroll_line(
    run_id: str, line_id: str, body: PayrollLineUpdate
) -> PayrollRunOut:
    with _canonical_404():
        return await runs.set_line_action(run_id, line_id, body.owner_action, body.note)


def _send_response(db: Session, run_id: str) -> PayrollSendResponse:
    """Built from the run as stored after the send."""
    db.expire_all()
    row = db.get(PayrollRunRow, run_id)
    if row is None or row.sender_batch_id is None or row.paypal_batch_id is None:
        raise ApiError(409, "RUN_NOT_SENDABLE", "This payroll run has no PayPal batch.")
    out = runs.get_run_out(run_id)
    sent = sum(1 for ln in out.lines if ln.payout_status is not None)
    return PayrollSendResponse(
        run_id=run_id,
        status="SENT",
        sender_batch_id=row.sender_batch_id,
        paypal_batch_id=row.paypal_batch_id,
        batch_status=row.batch_status or "PENDING",
        total=out.total,
        lines_sent=sent,
        lines_excluded=len(out.lines) - sent,
    )


def _already_sent_with_key(db: Session, run_id: str, key: str) -> bool:
    row = db.get(PayrollRunRow, run_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Payroll run not found.")
    return row.idempotency_key == key and row.paypal_batch_id is not None


@router.post(
    "/payroll/runs/{run_id}/send",
    response_model=PayrollSendResponse,
    status_code=202,
    dependencies=_write,
)
async def send_payroll_run(
    run_id: str, body: PayrollSendRequest, db: Session = Depends(get_db)
) -> PayrollSendResponse:
    if _already_sent_with_key(db, run_id, body.idempotency_key):
        # A replay of the same send: PayPal is not called again, polling is idempotent.
        payouts_sync.start_polling(run_id)
        return _send_response(db, run_id)
    ready = await runs.lines_ready_to_send(run_id, body.approve_run_flags)
    current = runs.get_run_out(run_id)
    if sum((ln.gross for ln in ready), _ZERO) != current.total:
        raise ApiError(
            409, "RUN_NOT_SENDABLE", "The payroll run changed. Refresh it and try again."
        )
    await payouts.send_payroll_batch(
        run_id, ready, body.idempotency_key, approve_run_flags=body.approve_run_flags
    )
    payouts_sync.start_polling(run_id)
    return _send_response(db, run_id)


# --- margin ------------------------------------------------------------------------------


@router.get("/ledger/margin", response_model=MarginResponse)
def ledger_margin(db: Session = Depends(get_db)) -> MarginResponse:
    return runs.margin_rows(db)


__all__ = ["leash_models", "router"]
