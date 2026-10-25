"""Tests for the mapping cache and typed timesheet entries (plan WP-5.3, TST-17)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal

import pytest

from backend.app.leash.payroll.mapping import (
    InMemoryMappingStore,
    Mapper,
    confirm_mapping,
    guess_mapping,
    resolve_mapping,
)
from backend.app.leash.payroll.parse import RawTable, TimesheetError, read_table
from backend.app.leash.payroll.timesheet import parse_entries
from backend.app.schemas.contracts import ColumnMappingProposal, TimesheetField

DEMO_CSV = (
    b"Employee,Day,Hrs,Project\n"
    b"Omar Hassan,2026-10-01,8,MS-1\n"
    b"Omar Hassan,2026-10-02,7.5,MS-1\n"
    b"Sara Ali,2026-10-01,6,MS-1\n"
    b"Sara Ali,2026-10-02,6,MS-1\n"
    b"Youssef Adel,2026-10-01,4,MS-2\n"
    b"Youssef Adel,2026-10-02,5,MS-2\n"
    b"Omar Hassan,2026-10-03,3,MS-2\n"
    b"Sara Ali,2026-10-03,2.25,MS-2\n"
    b"Youssef Adel,2026-10-03,8,MS-2\n"
)
DEMO_MAPPING: dict[str, TimesheetField] = {
    "Employee": "member",
    "Day": "work_date",
    "Hrs": "hours",
    "Project": "milestone_code",
}
HEADERS = ("Employee", "Day", "Hrs", "Project")


class FakeMapper:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[list[str]]]] = []

    async def __call__(
        self, headers: list[str], samples: list[list[str]]
    ) -> ColumnMappingProposal:
        self.calls.append((headers, samples))
        return ColumnMappingProposal(
            mapping=dict(DEMO_MAPPING), confidence={h: 0.9 for h in DEMO_MAPPING}
        )


def _table(*rows: tuple[object, ...]) -> RawTable:
    return RawTable(headers=HEADERS, rows=tuple(rows))


def _row(
    name: object = "Omar",
    day: object = "2026-10-01",
    hrs: object = 8,
    ms: object = "MS-1",
) -> tuple[object, ...]:
    return (name, day, hrs, ms)


# ---------------------------------------------------------------- mapping


@pytest.mark.asyncio
async def test_known_layout_makes_zero_mapper_calls() -> None:
    table = read_table(DEMO_CSV, "t.csv")
    store = InMemoryMappingStore()
    mapper = FakeMapper()

    first = await resolve_mapping(table, store, mapper)
    assert first.known is False
    assert first.mapping is None
    assert first.proposal is not None
    assert len(mapper.calls) == 1
    assert mapper.calls[0][0] == list(HEADERS)
    assert len(mapper.calls[0][1]) == 5

    await confirm_mapping(store, first.signature, dict(DEMO_MAPPING))

    second = await resolve_mapping(table, store, mapper)
    assert second.known is True
    assert second.mapping == DEMO_MAPPING
    assert second.signature == first.signature
    assert len(mapper.calls) == 1


class _Raises:
    async def __call__(self, h: list[str], s: list[list[str]]) -> ColumnMappingProposal:
        raise RuntimeError("model down")


class _Slow:
    async def __call__(self, h: list[str], s: list[list[str]]) -> ColumnMappingProposal:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")


class _WrongType:
    async def __call__(self, h: list[str], s: list[list[str]]) -> object:
        return {"mapping": {}}


class _UnknownKeys:
    async def __call__(self, h: list[str], s: list[list[str]]) -> ColumnMappingProposal:
        return ColumnMappingProposal(mapping={"Nope": "member"}, confidence={})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapper", [_Raises(), _Slow(), None, _WrongType(), _UnknownKeys()]
)
async def test_mapper_failure_falls_back_to_heuristics(mapper: object) -> None:
    table = read_table(DEMO_CSV, "t.csv")
    res = await resolve_mapping(
        table,
        InMemoryMappingStore(),
        mapper,
        timeout_s=0.01,  # type: ignore[arg-type]
    )
    assert res.known is False
    assert res.proposal == guess_mapping(HEADERS)
    assert res.proposal.mapping == DEMO_MAPPING
    assert set(res.proposal.confidence.values()) == {0.6}


def test_guess_mapping_each_field_once_first_match() -> None:
    p = guess_mapping(["Staff Name", "Work Date", "Hours", "Hourly Rate", "Milestone"])
    assert p.mapping == {
        "Staff Name": "member",
        "Work Date": "work_date",
        "Hours": "hours",
        "Hourly Rate": "rate",
        "Milestone": "milestone_code",
    }
    p2 = guess_mapping(["Name", "Employee"])
    assert p2.mapping == {"Name": "member"}


@pytest.mark.asyncio
async def test_sample_rows_are_stringified_and_escaped() -> None:
    seen: list[list[list[str]]] = []

    async def mapper(h: list[str], s: list[list[str]]) -> ColumnMappingProposal:
        seen.append(s)
        return guess_mapping(h)

    mp: Mapper = mapper
    rows = tuple((f"=cmd{i}", datetime(2026, 10, 1, 9, 0), 7.3, None) for i in range(7))
    await resolve_mapping(_table(*rows), InMemoryMappingStore(), mp)
    assert len(seen[0]) == 5
    assert seen[0][0] == ["'=cmd0", "2026-10-01T09:00:00", "7.3", ""]


@pytest.mark.asyncio
async def test_confirm_requires_all_required_fields() -> None:
    store = InMemoryMappingStore()
    with pytest.raises(TimesheetError) as ei:
        await confirm_mapping(store, "sig", {"Employee": "member", "Hrs": "hours"})
    assert ei.value.code == "TIMESHEET_INVALID"
    assert await store.get("sig") is None


@pytest.mark.asyncio
async def test_confirm_rejects_duplicate_targets() -> None:
    store = InMemoryMappingStore()
    bad = {**DEMO_MAPPING, "Extra": "hours"}
    with pytest.raises(TimesheetError) as ei:
        await confirm_mapping(store, "sig", bad)
    assert ei.value.code == "TIMESHEET_INVALID"
    assert await store.get("sig") is None


@pytest.mark.asyncio
async def test_confirm_rejects_unknown_field() -> None:
    with pytest.raises(TimesheetError):
        await confirm_mapping(
            InMemoryMappingStore(), "sig", {**DEMO_MAPPING, "X": "salary"}
        )


# ---------------------------------------------------------------- entries


def test_parse_entries_demo() -> None:
    entries = parse_entries(read_table(DEMO_CSV, "t.csv"), DEMO_MAPPING)
    assert len(entries) == 9
    assert [e.row_number for e in entries] == list(range(2, 11))
    assert entries[0].member_name == "Omar Hassan"
    assert entries[0].member_key == "omar hassan"
    assert entries[0].work_date == date(2026, 10, 1)
    assert entries[0].hours == Decimal("8.00")
    assert entries[0].milestone_code == "MS-1"
    assert entries[0].rate_in_file is None
    assert entries[1].hours == Decimal("7.50")
    assert entries[7].hours == Decimal("2.25")
    assert entries[8].member_name == "Youssef Adel"
    assert entries[8].milestone_code == "MS-2"
    assert sum((e.hours for e in entries), Decimal(0)) == Decimal("49.75")


def test_unreadable_rows_rejected_with_row_numbers() -> None:
    table = _table(
        _row(),  # row 2 fine
        _row(day="31/02/2026"),  # row 3 bad date
        _row(hrs="8h"),  # row 4
        _row(hrs="=1+1"),  # row 5
        _row(ms="phase one"),  # row 6
        _row(hrs="1.234"),  # row 7
        _row(),  # row 8 fine
    )
    with pytest.raises(TimesheetError) as ei:
        parse_entries(table, DEMO_MAPPING)
    assert ei.value.code == "TIMESHEET_INVALID"
    assert ei.value.details is not None
    rows = ei.value.details["rows"]
    assert [r["row"] for r in rows] == [3, 4, 5, 6, 7]
    assert "unreadable date" in rows[0]["error"]
    assert "=1+1" not in str(rows)


def test_row_errors_capped_at_50() -> None:
    table = _table(*[_row(hrs="x") for _ in range(80)])
    with pytest.raises(TimesheetError) as ei:
        parse_entries(table, DEMO_MAPPING)
    assert ei.value.details is not None
    assert len(ei.value.details["rows"]) == 50


def test_xlsx_float_cells_become_exact_decimals() -> None:
    (e,) = parse_entries(_table(_row(hrs=7.3)), DEMO_MAPPING)
    assert e.hours == Decimal("7.30")
    assert str(e.hours) == "7.30"


def test_day_first_dates() -> None:
    (e,) = parse_entries(_table(_row(day="05/11/2026")), DEMO_MAPPING)
    assert e.work_date == date(2026, 11, 5)


def test_date_and_datetime_cells() -> None:
    a, b = parse_entries(
        _table(
            _row(day=date(2026, 10, 1)),
            _row(day=datetime(2026, 10, 2, 13, 30)),
        ),
        DEMO_MAPPING,
    )
    assert a.work_date == date(2026, 10, 1)
    assert b.work_date == date(2026, 10, 2)


@pytest.mark.parametrize("day", [45000, 45000.5, "2026-13-01", "1/2/2026", "", None])
def test_bad_dates_rejected(day: object) -> None:
    with pytest.raises(TimesheetError):
        parse_entries(_table(_row(day=day)), DEMO_MAPPING)


def test_hours_rules() -> None:
    ok = parse_entries(
        _table(
            _row(hrs="-2.5"), _row(hrs=0), _row(hrs=" 999.99 "), _row(hrs=Decimal(3))
        ),
        DEMO_MAPPING,
    )
    assert [e.hours for e in ok] == [
        Decimal("-2.50"),
        Decimal("0.00"),
        Decimal("999.99"),
        Decimal("3.00"),
    ]
    for bad in ("1000", 1000, "1,5", "", None, "1e2", float("nan"), "+8"):
        with pytest.raises(TimesheetError):
            parse_entries(_table(_row(hrs=bad)), DEMO_MAPPING)


def test_milestone_uppercased_and_stripped() -> None:
    (e,) = parse_entries(_table(_row(ms="  ms-12 ")), DEMO_MAPPING)
    assert e.milestone_code == "MS-12"


def test_rate_column() -> None:
    mapping: dict[str, TimesheetField] = {**DEMO_MAPPING, "Rate": "rate"}
    table = RawTable(
        headers=(*HEADERS, "Rate"),
        rows=(
            (*_row(), 25),
            (*_row(), None),
            (*_row(), "22.50"),
            (*_row(), 22.5),
            (*_row(), "  "),
        ),
    )
    entries = parse_entries(table, mapping)
    assert [e.rate_in_file for e in entries] == [
        Decimal("25.00"),
        None,
        Decimal("22.50"),
        Decimal("22.50"),
        None,
    ]
    for bad in (0, "-1", "1.234", 1_000_000, "abc"):
        bad_table = RawTable(headers=table.headers, rows=((*_row(), bad),))
        with pytest.raises(TimesheetError):
            parse_entries(bad_table, mapping)


def test_missing_name_is_row_error() -> None:
    with pytest.raises(TimesheetError) as ei:
        parse_entries(_table(_row(name="   ")), DEMO_MAPPING)
    assert ei.value.details is not None
    assert ei.value.details["rows"][0]["error"] == "missing name"


def test_mapping_must_match_table() -> None:
    with pytest.raises(TimesheetError):
        parse_entries(_table(_row()), {**DEMO_MAPPING, "Ghost": "rate"})
    with pytest.raises(TimesheetError):
        parse_entries(_table(_row()), {"Employee": "member"})


def test_escaped_name_still_matches_roster() -> None:
    (e,) = parse_entries(_table(_row(name="=Omar")), DEMO_MAPPING)
    assert e.member_name == "'=Omar"
    assert e.member_key == "=omar"


def test_name_matching_is_case_and_space_insensitive() -> None:
    a, b = parse_entries(_table(_row(name="omar"), _row(name=" Omar ")), DEMO_MAPPING)
    assert a.member_key == b.member_key == "omar"
    assert b.member_name == "Omar"
