"""Decimal pay calculation for one milestone.

Gross is hours x the roster rate (never the rate in the file), quantized to cents with
ROUND_HALF_UP. A line with any entry of hours <= 0 or an unknown member has gross 0.00
(``hours`` still reports the true sum), so it can never reduce the run total.
No float, no I/O, no clock reads. All datetimes elsewhere must be timezone-aware UTC.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..roster import InMemoryRoster, TeamMember
from .timesheet import TimesheetEntry

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_PCT_CAP = Decimal("999.99")
_MILESTONE_CODE = re.compile(r"([A-Z]+)-(\d+)", re.ASCII)


def milestone_key(code: str) -> tuple[str, int]:
    """Canonical milestone identity: (UPPER prefix, number), so MS-1 equals MS-01.

    Raises ValueError for a code that is not ``<LETTERS>-<ASCII digits>``. Callers
    fail closed: ``build_lines`` matches nothing for such a code, and PR-07 falls back
    to comparing the normalised (stripped, upper-cased) strings.
    """
    found = _MILESTONE_CODE.fullmatch(code.strip().upper())
    if found is None:
        msg = f"unparseable milestone code: {code!r}"
        raise ValueError(msg)
    return found.group(1), int(found.group(2))


def _key_or_none(code: str) -> tuple[str, int] | None:
    try:
        return milestone_key(code)
    except ValueError:
        return None


@dataclass(frozen=True)
class DraftLine:
    member_key: str
    member: TeamMember | None
    display_name: str
    entries: tuple[TimesheetEntry, ...]
    hours: Decimal
    rate: Decimal | None
    gross: Decimal


def build_lines(
    entries: Sequence[TimesheetEntry], milestone_code: str, roster: InMemoryRoster
) -> tuple[DraftLine, ...]:
    """One line per member_key for the milestone, in first-seen order."""
    wanted = _key_or_none(milestone_code)
    groups: dict[str, list[TimesheetEntry]] = {}
    for entry in entries:
        if wanted is not None and _key_or_none(entry.milestone_code) == wanted:
            groups.setdefault(entry.member_key, []).append(entry)

    lines: list[DraftLine] = []
    for key, group in groups.items():
        member = roster.find_by_key(key)
        hours = sum((e.hours for e in group), Decimal("0.00"))
        if member is None:
            rate: Decimal | None = None
            gross = _ZERO
            display = group[0].member_name
        else:
            rate = member.hourly_rate
            if any(e.hours <= 0 for e in group):
                gross = _ZERO
            else:
                gross = (hours * rate).quantize(_CENT, rounding=ROUND_HALF_UP)
            display = member.name
        lines.append(
            DraftLine(
                member_key=key,
                member=member,
                display_name=display,
                entries=tuple(group),
                hours=hours,
                rate=rate,
                gross=gross,
            )
        )
    return tuple(lines)


def run_total(
    lines: Sequence[DraftLine], excluded: Collection[str], denied: Collection[str]
) -> Decimal:
    """Sum of gross over lines that are neither excluded nor denied (by member_key)."""
    total = _ZERO
    for line in lines:
        if line.gross <= 0 or line.member_key in excluded or line.member_key in denied:
            continue
        total += line.gross
    return total


def cash_used_pct(total: Decimal, cash_in: Decimal) -> Decimal:
    """Share of cash in spent on this run, 2 dp, clamped to 0.00..999.99.

    A total <= 0 is 0.00. A positive total against cash_in <= 0 is 999.99.
    """
    if total <= 0:
        return _ZERO
    if cash_in <= 0:
        return _PCT_CAP
    pct = (total / cash_in * 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    return min(pct, _PCT_CAP)
