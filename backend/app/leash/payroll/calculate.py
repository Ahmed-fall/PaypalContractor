"""Decimal pay calculation for one milestone.

Gross is hours x the roster rate (never the rate in the file), quantized to cents with
ROUND_HALF_UP. No float, no I/O, no clock reads.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..roster import InMemoryRoster, TeamMember
from .timesheet import TimesheetEntry

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_PCT_CAP = Decimal("999.99")


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
    wanted = milestone_code.upper()
    groups: dict[str, list[TimesheetEntry]] = {}
    for entry in entries:
        if entry.milestone_code.upper() == wanted:
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
        if line.member_key in excluded or line.member_key in denied:
            continue
        total += line.gross
    return total


def cash_used_pct(total: Decimal, cash_in: Decimal) -> Decimal:
    """Share of cash in spent on this run, 2 dp, capped at 999.99."""
    if cash_in == 0:
        return _PCT_CAP if total > 0 else _ZERO
    pct = (total / cash_in * 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    return min(pct, _PCT_CAP)
