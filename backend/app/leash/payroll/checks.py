"""Payroll checks PR-01..PR-07 (plan section 5.3).

Pure and synchronous: histories, policy and ``now`` are passed in. A line's decision is
the strictest of its flags (DENY > ASK > ALLOW); flags are unique and sorted by id.
All datetimes must be timezone-aware UTC.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from ...schemas.contracts import LeashDecision, LeashPolicy
from .calculate import DraftLine

RUN_KEY = "__run__"
MAX_HOURS_PER_DAY = Decimal(12)
MAX_HOURS_PER_WEEK = Decimal(60)

_DENY_FLAGS = frozenset({"PR-05", "PR-07"})


@dataclass(frozen=True)
class MemberHistory:
    last_paid_at: datetime | None
    paid_milestones: frozenset[str]


@dataclass(frozen=True)
class LineCheck:
    member_key: str
    decision: LeashDecision
    flags: tuple[str, ...]


def _pr01(
    line: DraftLine, history: MemberHistory, policy: LeashPolicy, now: datetime
) -> bool:
    member = line.member
    if member is None or member.email_changed_at is None:
        return False
    changed = member.email_changed_at
    if now - changed <= timedelta(days=policy.email_change_window_days):
        return True
    return history.last_paid_at is not None and changed > history.last_paid_at


def _pr02_deny(line: DraftLine) -> bool:
    return any(e.hours <= 0 for e in line.entries)


def _pr02_ask(line: DraftLine) -> bool:
    per_day: dict[date, Decimal] = {}
    per_week: dict[tuple[int, int], Decimal] = {}
    for e in line.entries:
        iso = e.work_date.isocalendar()
        per_day[e.work_date] = per_day.get(e.work_date, Decimal(0)) + e.hours
        wk = (iso.year, iso.week)
        per_week[wk] = per_week.get(wk, Decimal(0)) + e.hours
    return any(v > MAX_HOURS_PER_DAY for v in per_day.values()) or any(
        v > MAX_HOURS_PER_WEEK for v in per_week.values()
    )


def _has_duplicates(line: DraftLine) -> bool:
    keys = Counter((e.work_date, e.milestone_code.upper()) for e in line.entries)
    return any(n > 1 for n in keys.values())


def _decision(flags: tuple[str, ...], deny: bool) -> LeashDecision:
    if deny or _DENY_FLAGS.intersection(flags):
        return LeashDecision.DENY
    return LeashDecision.ASK if flags else LeashDecision.ALLOW


def check_line(
    line: DraftLine,
    history: MemberHistory,
    milestone_code: str,
    policy: LeashPolicy,
    now: datetime,
) -> LineCheck:
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    paid = {c.strip().upper() for c in history.paid_milestones}
    flags: set[str] = set()
    deny = False

    if line.member is None or _has_duplicates(line):
        flags.add("PR-05")
    if _pr02_deny(line):
        flags.add("PR-02")
        deny = True
    elif _pr02_ask(line):
        flags.add("PR-02")

    if line.member is not None:
        if _pr01(line, history, policy, now):
            flags.add("PR-01")
        if line.member.first_paid_at is None:
            flags.add("PR-03")
        if any(
            e.rate_in_file is not None and e.rate_in_file != line.member.hourly_rate
            for e in line.entries
        ):
            flags.add("PR-04")
        if milestone_code.strip().upper() in paid:
            flags.add("PR-07")

    ordered = tuple(sorted(flags))
    return LineCheck(line.member_key, _decision(ordered, deny), ordered)


def check_run(total: Decimal, cash_in: Decimal, policy: LeashPolicy) -> LineCheck:
    """PR-06: the run spends more than the margin limit of cash in (strictly greater)."""
    limit = cash_in * policy.payroll_margin_limit_pct / 100
    if total > limit:
        return LineCheck(RUN_KEY, LeashDecision.ASK, ("PR-06",))
    return LineCheck(RUN_KEY, LeashDecision.ALLOW, ())
