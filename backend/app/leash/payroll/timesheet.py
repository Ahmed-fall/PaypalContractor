"""Turn a raw table plus a confirmed column mapping into typed timesheet entries.

Numbers and dates are parsed from the raw cell, never evaluated, so a formula in
``hours`` is a row error. Any bad row rejects the whole upload, listing the rows.
Text kept for storage is escaped; ``member_key`` is computed from the raw name so
it still matches the roster. Whether the hours are sensible is PR-02's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ...schemas.contracts import TimesheetField
from ..roster import normalise_name
from .mapping import REQUIRED_FIELDS
from .parse import RawTable, TimesheetError, escape_cell, normalise_header

MAX_ROW_ERRORS = 50
_CENT = Decimal("0.01")
MAX_NAME_LEN = 120  # team_members.name, A.3
MAX_MILESTONE_LEN = 16  # milestone code column, A.3
# ASCII only: str \d also matches other scripts' digits (e.g. Arabic-Indic).
_NUMBER = re.compile(r"-?\d+(\.\d{1,2})?", re.ASCII)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DAY_FIRST = re.compile(r"\d{2}/\d{2}/\d{4}", re.ASCII)
_MILESTONE = re.compile(r"[A-Z]+-\d+", re.ASCII)


@dataclass(frozen=True)
class TimesheetEntry:
    row_number: int
    member_name: str
    member_key: str
    work_date: date
    hours: Decimal
    milestone_code: str
    rate_in_file: Decimal | None


class _RowError(Exception):
    pass


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _decimal(value: object, what: str) -> Decimal:
    """Exact Decimal from a number or numeric text, at most 2 dp, |v| < 1,000,000."""
    if isinstance(value, bool):
        raise _RowError(f"unreadable {what}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not _NUMBER.fullmatch(text):
            raise _RowError(f"unreadable {what}")
        d = Decimal(text)
    else:
        raise _RowError(f"unreadable {what}")
    if not d.is_finite() or abs(d) >= 1_000_000:
        raise _RowError(f"unreadable {what}")
    q = d.quantize(_CENT, rounding=ROUND_HALF_UP)
    if q != d:
        raise _RowError(f"{what} has more than 2 decimal places")
    return q


def _hours(value: object) -> Decimal:
    if _blank(value):
        raise _RowError("missing hours")
    d = _decimal(value, "hours")
    if abs(d) >= 1000:
        raise _RowError("hours out of range")
    return d


def _rate(value: object) -> Decimal | None:
    if _blank(value):
        return None
    d = _decimal(value, "rate")
    if d <= 0:
        raise _RowError("rate must be greater than 0")
    return d


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if _blank(value):
        raise _RowError("missing date")
    if isinstance(value, str):
        text = value.strip()
        try:
            if _ISO_DATE.fullmatch(text):
                return date.fromisoformat(text)
            if _DAY_FIRST.fullmatch(text):
                return datetime.strptime(text, "%d/%m/%Y").date()
        except ValueError:
            pass
    raise _RowError("unreadable date")


def _text(value: object) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def parse_entries(
    table: RawTable, mapping: dict[str, TimesheetField]
) -> tuple[TimesheetEntry, ...]:
    """Typed entries for every row, or `TIMESHEET_INVALID` listing the bad rows."""
    by_name = {normalise_header(h): i for i, h in enumerate(table.headers)}
    index: dict[str, int] = {}
    for header, field in mapping.items():
        position = by_name.get(normalise_header(header))
        if position is None:
            raise TimesheetError(
                "TIMESHEET_INVALID", "The column mapping does not match this timesheet."
            )
        index[field] = position
    missing = sorted(REQUIRED_FIELDS - set(index))
    if missing:
        raise TimesheetError(
            "TIMESHEET_INVALID",
            "Map a column to every required field: " + ", ".join(missing) + ".",
            {"missing": missing},
        )

    entries: list[TimesheetEntry] = []
    errors: list[dict[str, Any]] = []
    for offset, row in enumerate(table.rows):
        number = offset + 2  # the header is row 1
        problems: list[str] = []
        work_date = date.min
        hours = Decimal(0)
        rate: Decimal | None = None

        raw_name = _text(row[index["member"]])
        if not raw_name:
            problems.append("missing name")
        elif len(escape_cell(raw_name)) > MAX_NAME_LEN:
            problems.append("name too long")
        try:
            work_date = _date(row[index["work_date"]])
        except _RowError as exc:
            problems.append(str(exc))
        try:
            hours = _hours(row[index["hours"]])
        except _RowError as exc:
            problems.append(str(exc))
        code = _text(row[index["milestone_code"]]).upper()
        if not code:
            problems.append("missing milestone")
        elif len(code) > MAX_MILESTONE_LEN:
            problems.append("milestone too long")
        elif not _MILESTONE.fullmatch(code):
            problems.append("unreadable milestone")
        if "rate" in index:
            try:
                rate = _rate(row[index["rate"]])
            except _RowError as exc:
                problems.append(str(exc))

        if problems:
            if len(errors) < MAX_ROW_ERRORS:
                errors.append({"row": number, "error": "; ".join(problems)})
            continue
        entries.append(
            TimesheetEntry(
                row_number=number,
                member_name=escape_cell(raw_name),
                member_key=normalise_name(raw_name),
                work_date=work_date,
                hours=hours,
                milestone_code=code,
                rate_in_file=rate,
            )
        )

    if errors:
        raise TimesheetError(
            "TIMESHEET_INVALID",
            "Some timesheet rows could not be read.",
            {"rows": errors},
        )
    return tuple(entries)
