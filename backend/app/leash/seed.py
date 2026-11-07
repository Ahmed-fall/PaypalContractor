"""Idempotent Leash demo seed: roster, policy v1, a confirmed mapping, MS-01 hours.

The roster mirrors the demo story: Omar's payout email changed yesterday (PR-01 fires
on his line); Sara and Youssef are long-standing. The parsed MS-01 timesheet gives
Omar 12, Sara 10 and Youssef 8 hours. Run it twice and the row counts stay the same.
``reset_leash_demo`` clears that state again for ``/demo/reset``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..db.database import new_id
from .models import (
    ColumnMappingRow,
    LeashPolicyRow,
    PayrollLineRow,
    PayrollRunRow,
    TeamMemberRow,
    TimesheetEntryRow,
    TimesheetUploadRow,
)
from .payroll.parse import header_signature
from .policy import default_policy
from .store import DEFAULT_POLICY_SOURCE

FIRST_PAID_AT = datetime(2026, 9, 30, tzinfo=UTC)
MILESTONE_CODE = "MS-01"
HEADERS = ("Employee", "Day", "Hrs", "Project")
MAPPING = {
    "Employee": "member",
    "Day": "work_date",
    "Hrs": "hours",
    "Project": "milestone_code",
}
# (name, payout email, hourly rate, email changed one day ago?)
ROSTER = (
    ("Omar", "omar-test-account@personal.example.com", "25.00", True),
    ("Sara", "sara-test-account@personal.example.com", "22.00", False),
    ("Youssef", "youssef-test-account@personal.example.com", "25.00", False),
)
# (name, hours per day) on 2026-11-02, -03 and -04.
HOURS = (
    ("Omar", ("4", "4", "4")),
    ("Sara", ("4", "3", "3")),
    ("Youssef", ("3", "3", "2")),
)
_DAYS = (date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4))
_FILE_NAME = "demo-timesheet-ms-01.csv"


def _csv_bytes() -> bytes:
    lines = [",".join(HEADERS)]
    for name, per_day in HOURS:
        lines.extend(
            f"{name},{day.isoformat()},{h},{MILESTONE_CODE}"
            for day, h in zip(_DAYS, per_day, strict=True)
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def seed_leash_demo(db: Session, *, now: datetime) -> None:
    members: dict[str, TeamMemberRow] = {}
    for name, email, rate, changed in ROSTER:
        row = db.scalars(
            select(TeamMemberRow).where(TeamMemberRow.name == name, TeamMemberRow.active.is_(True))
        ).first()
        if row is None:
            row = TeamMemberRow(
                id=new_id("tm"),
                name=name,
                payout_email=email,
                hourly_rate=Decimal(rate),
                email_changed_at=now - timedelta(days=1) if changed else None,
                first_paid_at=FIRST_PAID_AT,
                currency="USD",
                active=True,
                created_at=now - timedelta(days=60),
            )
            db.add(row)
        members[name] = row

    if db.scalars(select(LeashPolicyRow).where(LeashPolicyRow.version == 1)).first() is None:
        policy = default_policy()
        db.add(
            LeashPolicyRow(
                id=new_id("lpol"),
                version=policy.version,
                source_text=DEFAULT_POLICY_SOURCE,
                compiled_json=policy.model_dump(mode="json"),
                active=True,
                created_at=now,
                activated_at=now,
            )
        )

    signature = header_signature(HEADERS)
    if (
        db.scalars(
            select(ColumnMappingRow).where(ColumnMappingRow.header_signature == signature)
        ).first()
        is None
    ):
        db.add(
            ColumnMappingRow(
                id=new_id("cm"),
                header_signature=signature,
                mapping_json=dict(MAPPING),
                confirmed_at=now,
            )
        )

    data = _csv_bytes()
    file_hash = hashlib.sha256(data).hexdigest()
    upload = db.scalars(
        select(TimesheetUploadRow).where(TimesheetUploadRow.file_hash == file_hash)
    ).first()
    if upload is None:
        upload = TimesheetUploadRow(
            id=new_id("tsu"),
            file_name=_FILE_NAME,
            file_hash=file_hash,
            header_signature=signature,
            row_count=sum(len(per_day) for _, per_day in HOURS),
            status="PARSED",
            created_at=now,
        )
        db.add(upload)
        db.flush()
        for name, per_day in HOURS:
            for day, hours in zip(_DAYS, per_day, strict=True):
                db.add(
                    TimesheetEntryRow(
                        id=new_id("tse"),
                        upload_id=upload.id,
                        team_member_id=members[name].id,
                        raw_member_name=name,
                        work_date=day,
                        hours=Decimal(hours),
                        milestone_code=MILESTONE_CODE,
                    )
                )
    db.commit()


# Children before parents: lines -> runs (-> milestones), entries -> uploads, members last.
_RESET_ORDER = (
    PayrollLineRow,
    PayrollRunRow,
    TimesheetEntryRow,
    TimesheetUploadRow,
    ColumnMappingRow,
    TeamMemberRow,
)


def reset_leash_demo(db: Session) -> dict[str, int]:
    """Delete the Leash demo state so the next take starts clean; returns rows per table.

    Runs before the Detective wipe, because ``payroll_runs.milestone_id`` references
    ``milestones``. Kept on purpose:

    - ``leash_decisions``: the append-only audit log (A.3). The deleted runs and
      lines only reference decisions; no kept row references a deleted one.
    - ``leash_policies``: kept because every decision references its version; the
      seeded v1 becomes the active policy again.

    In-process state needs no reset: the H2/poll tasks and run locks are keyed by
    run ids, which are fresh ULIDs and never reused, and a poller whose run is gone
    stops on its next tick.
    """
    wiped: dict[str, int] = {}
    for model in _RESET_ORDER:
        result = db.execute(delete(model))
        wiped[model.__tablename__] = int(getattr(result, "rowcount", 0) or 0)
    seeded = LeashPolicyRow.version == 1
    if db.scalars(
        select(LeashPolicyRow.id).where(seeded, LeashPolicyRow.active.is_(False))
    ).first():
        # Two statements: the one-active-policy unique index must never see two rows.
        db.execute(update(LeashPolicyRow).where(~seeded).values(active=False))
        db.execute(update(LeashPolicyRow).where(seeded).values(active=True))
    db.commit()
    return wiped
