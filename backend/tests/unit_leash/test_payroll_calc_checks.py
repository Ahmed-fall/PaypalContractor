"""Pay calculation and payroll checks PR-01..PR-07 (TST-19 demo half)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.leash.payroll.calculate import (
    DraftLine,
    build_lines,
    cash_used_pct,
    milestone_key,
    run_total,
)
from backend.app.leash.payroll.checks import (
    MemberHistory,
    check_line,
    check_run,
)
from backend.app.leash.payroll.timesheet import TimesheetEntry
from backend.app.leash.policy import default_policy
from backend.app.leash.roster import InMemoryRoster, TeamMember, normalise_name
from backend.app.schemas.contracts import LeashDecision

NOW = datetime(2026, 11, 4, 18, 40, tzinfo=UTC)
PAID_BEFORE = datetime(2026, 9, 30, tzinfo=UTC)
POLICY = default_policy()
D = Decimal
ALLOW, ASK, DENY = LeashDecision.ALLOW, LeashDecision.ASK, LeashDecision.DENY
HIST = MemberHistory(last_paid_at=PAID_BEFORE, paid_milestones=frozenset())


def _member(
    name: str,
    rate: str = "25.00",
    changed: datetime | None = None,
    first_paid: datetime | None = PAID_BEFORE,
) -> TeamMember:
    return TeamMember(
        member_id="tm_" + name.lower(),
        name=name,
        payout_email=f"{name.lower()}@example.com",
        hourly_rate=D(rate),
        email_changed_at=changed,
        first_paid_at=first_paid,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def _entry(
    name: str = "Omar",
    day: date = date(2026, 11, 2),
    hours: str = "4.00",
    ms: str = "MS-01",
    file_rate: str | None = None,
    row: int = 2,
) -> TimesheetEntry:
    return TimesheetEntry(
        row_number=row,
        member_name=name,
        member_key=normalise_name(name),
        work_date=day,
        hours=D(hours),
        milestone_code=ms,
        rate_in_file=D(file_rate) if file_rate is not None else None,
    )


def _line(member: TeamMember | None, entries: list[TimesheetEntry]) -> DraftLine:
    roster = InMemoryRoster()
    if member is not None:
        roster.add(member)
    return build_lines(entries, "MS-01", roster)[0]


def _check(
    member: TeamMember | None,
    entries: list[TimesheetEntry],
    history: MemberHistory = HIST,
) -> tuple[LeashDecision, tuple[str, ...]]:
    res = check_line(_line(member, entries), history, "MS-01", POLICY, NOW)
    return res.decision, res.flags


def test_demo_run_flags_exactly_omar_and_run() -> None:
    roster = InMemoryRoster()
    roster.add(_member("Omar", changed=NOW - timedelta(days=1)))
    roster.add(_member("Sara", "22.00"))
    roster.add(_member("Youssef"))
    entries = [
        _entry("Omar", date(2026, 11, 2), "6.00", row=2),
        _entry("Omar", date(2026, 11, 3), "6.00", row=3),
        _entry("Sara", date(2026, 11, 2), "5.00", row=4),
        _entry("Sara", date(2026, 11, 3), "5.00", row=5),
        _entry("Youssef", date(2026, 11, 2), "4.00", row=6),
        _entry("Youssef", date(2026, 11, 3), "4.00", row=7),
    ]
    lines = build_lines(entries, "MS-01", roster)
    assert [ln.gross for ln in lines] == [D("300.00"), D("220.00"), D("200.00")]
    assert [ln.display_name for ln in lines] == ["Omar", "Sara", "Youssef"]
    cash_in = D("850.00")
    total = run_total(lines, excluded=[], denied=[])
    assert total == D("720.00")
    assert cash_used_pct(total, cash_in) == D("84.71")

    results = [check_line(ln, HIST, "MS-01", POLICY, NOW) for ln in lines]
    assert [(r.decision, r.flags) for r in results] == [
        (ASK, ("PR-01",)),
        (ALLOW, ()),
        (ALLOW, ()),
    ]
    run = check_run(total, cash_in, POLICY)
    assert (run.decision, run.flags, run.member_key) == (ASK, ("PR-06",), "__run__")

    omar_key = lines[0].member_key
    total2 = run_total(lines, excluded=[omar_key], denied=[])
    assert total2 == D("420.00")
    assert cash_used_pct(total2, cash_in) == D("49.41")
    run2 = check_run(total2, cash_in, POLICY)
    assert (run2.decision, run2.flags) == (ALLOW, ())


_WEEK = [date(2026, 11, 2) + timedelta(days=i) for i in range(5)]  # Mon-Fri

CASES = [
    pytest.param(
        _member("Omar", changed=NOW - timedelta(days=3)),
        [_entry()],
        HIST,
        (ASK, ("PR-01",)),
        id="pr01-inside-window",
    ),
    pytest.param(
        _member("Omar", changed=NOW - timedelta(days=7)),
        [_entry()],
        HIST,
        (ASK, ("PR-01",)),
        id="pr01-boundary-7-days",
    ),
    pytest.param(
        _member("Omar", changed=NOW - timedelta(days=20)),
        [_entry()],
        MemberHistory(NOW - timedelta(days=30), frozenset()),
        (ASK, ("PR-01",)),
        id="pr01-outside-window-after-last-paid",
    ),
    pytest.param(
        _member("Omar", changed=NOW - timedelta(days=20)),
        [_entry()],
        MemberHistory(NOW - timedelta(days=10), frozenset()),
        (ALLOW, ()),
        id="pr01-outside-window-before-last-paid",
    ),
    pytest.param(
        _member("Omar", changed=NOW - timedelta(days=20)),
        [_entry()],
        MemberHistory(None, frozenset()),
        (ASK, ("PR-01",)),
        id="pr01-outside-window-no-history-falls-back-to-first-paid",
    ),
    pytest.param(_member("Omar"), [_entry()], HIST, (ALLOW, ()), id="pr01-never"),
    pytest.param(
        _member("Omar"), [_entry(hours="12.00")], HIST, (ALLOW, ()), id="pr02-12h"
    ),
    pytest.param(
        _member("Omar"),
        [_entry(hours="12.01")],
        HIST,
        (ASK, ("PR-02",)),
        id="pr02-12.01h",
    ),
    pytest.param(
        _member("Omar"),
        [
            _entry(hours="6.01", row=2),
            _entry(hours="6.00", day=date(2026, 11, 3), row=3),
        ],
        HIST,
        (ALLOW, ()),
        id="pr02-two-days-fine",
    ),
    pytest.param(
        _member("Omar"),
        [_entry(day=d, hours="12.20", row=i) for i, d in enumerate(_WEEK)],
        HIST,
        (ASK, ("PR-02",)),
        id="pr02-61h-week",
    ),
    pytest.param(
        _member("Omar"),
        [_entry(day=d, hours="12.00", row=i) for i, d in enumerate(_WEEK)],
        HIST,
        (ALLOW, ()),
        id="pr02-60h-week",
    ),
    pytest.param(
        _member("Omar"),
        [_entry(hours="0.00")],
        HIST,
        (DENY, ("PR-02",)),
        id="pr02-zero",
    ),
    pytest.param(
        _member("Omar"),
        [_entry(hours="-1.00"), _entry(hours="13.00", day=date(2026, 11, 3), row=3)],
        HIST,
        (DENY, ("PR-02",)),
        id="pr02-negative-and-over-single-flag",
    ),
    pytest.param(
        _member("Omar", first_paid=None),
        [_entry()],
        MemberHistory(None, frozenset()),
        (ASK, ("PR-03",)),
        id="pr03-never-paid",
    ),
    pytest.param(
        _member("Omar"),
        [_entry(file_rate="30.00")],
        HIST,
        (ASK, ("PR-04",)),
        id="pr04-rate-differs",
    ),
    pytest.param(
        _member("Omar"),
        [_entry(file_rate="25.00")],
        HIST,
        (ALLOW, ()),
        id="pr04-rate-equal",
    ),
    pytest.param(None, [_entry()], HIST, (DENY, ("PR-05",)), id="pr05-unknown"),
    pytest.param(
        None,
        [_entry(hours="13.00")],
        HIST,
        (DENY, ("PR-02", "PR-05")),
        id="pr05-unknown-with-pr02",
    ),
    pytest.param(
        _member("Omar"),
        [_entry(row=2), _entry(row=3)],
        HIST,
        (DENY, ("PR-05",)),
        id="pr05-duplicate-rows",
    ),
    pytest.param(
        _member("Omar"),
        [_entry()],
        MemberHistory(PAID_BEFORE, frozenset({"MS-01"})),
        (DENY, ("PR-07",)),
        id="pr07-already-paid",
    ),
    pytest.param(
        _member("Omar", changed=NOW - timedelta(days=1)),
        [_entry()],
        MemberHistory(PAID_BEFORE, frozenset({"MS-01"})),
        (DENY, ("PR-01", "PR-07")),
        id="combined-pr01-pr07",
    ),
]


@pytest.mark.parametrize(("member", "entries", "history", "expected"), CASES)
def test_each_check(
    member: TeamMember | None,
    entries: list[TimesheetEntry],
    history: MemberHistory,
    expected: tuple[LeashDecision, tuple[str, ...]],
) -> None:
    assert _check(member, entries, history) == expected


@pytest.mark.parametrize(
    ("total", "cash", "expected"),
    [
        ("800.00", "1000.00", (ALLOW, ())),
        ("800.01", "1000.00", (ASK, ("PR-06",))),
        ("0.00", "0.00", (ALLOW, ())),
        ("10.00", "0.00", (ASK, ("PR-06",))),
    ],
)
def test_pr06_run(
    total: str, cash: str, expected: tuple[LeashDecision, tuple[str, ...]]
) -> None:
    res = check_run(D(total), D(cash), POLICY)
    assert (res.decision, res.flags) == expected


def test_pr06_percentage_values() -> None:
    assert cash_used_pct(D("800.00"), D("1000.00")) == D("80.00")
    assert cash_used_pct(D("800.10"), D("1000.00")) == D("80.01")
    assert cash_used_pct(D("10.00"), D("0.00")) == D("999.99")


def test_gross_rounding_half_up() -> None:
    m = _member("Omar", "22.15")
    assert _line(m, [_entry(hours="7.33")]).gross == D("162.36")
    m2 = _member("Omar", "0.50")
    assert _line(m2, [_entry(hours="0.01")]).gross == D("0.01")


def test_milestone_matching_is_case_insensitive() -> None:
    roster = InMemoryRoster()
    roster.add(_member("Omar"))
    entries = [
        _entry(ms="ms-01", hours="2.00", row=2),
        _entry(ms="MS-02", hours="9.00", day=date(2026, 11, 3), row=3),
    ]
    (line,) = build_lines(entries, "Ms-01", roster)
    assert line.hours == D("2.00")
    assert len(line.entries) == 1
    assert build_lines(entries, "MS-09", roster) == ()


def test_unknown_member_line_and_display_name() -> None:
    roster = InMemoryRoster()
    (line,) = build_lines([_entry("=Evil", hours="3.00")], "MS-01", roster)
    assert line.member is None
    assert line.rate is None
    assert line.gross == D("0.00")
    assert line.hours == D("3.00")
    assert line.display_name == "=Evil"


def test_escaped_name_still_matches_roster() -> None:
    roster = InMemoryRoster()
    roster.add(_member("-Dash"))
    e = replace(_entry("-Dash"), member_name="'-Dash")
    (line,) = build_lines([e], "MS-01", roster)
    assert line.member is not None
    assert line.display_name == "-Dash"
    assert line.rate == D("25.00")


def test_run_total_excludes_and_denies() -> None:
    roster = InMemoryRoster()
    roster.add(_member("Omar"))
    roster.add(_member("Sara", "22.00"))
    entries = [
        _entry("Omar", hours="4.00", row=2),
        _entry("Sara", hours="5.00", row=3),
        _entry("Ghost", hours="9.00", row=4),
    ]
    lines = build_lines(entries, "MS-01", roster)
    assert run_total(lines, [], []) == D("210.00")
    assert run_total(lines, ["omar"], []) == D("110.00")
    assert run_total(lines, [], ["sara"]) == D("100.00")


def test_cash_used_pct_cap() -> None:
    assert cash_used_pct(D("1000000.00"), D("1.00")) == D("999.99")
    assert cash_used_pct(D("0.00"), D("0.00")) == D("0.00")
    assert cash_used_pct(D("0.00"), D("100.00")) == D("0.00")


def test_checks_are_pure() -> None:
    member = _member("Omar", changed=NOW - timedelta(days=1))
    entries = [_entry(), _entry(day=date(2026, 11, 3), row=3)]
    line = _line(member, entries)
    snapshot = (line, list(entries))
    a = check_line(line, HIST, "MS-01", POLICY, NOW)
    b = check_line(line, HIST, "MS-01", POLICY, NOW)
    assert a == b
    assert (line, entries) == snapshot
    assert check_run(D("1.00"), D("2.00"), POLICY) == check_run(
        D("1.00"), D("2.00"), POLICY
    )


def test_negative_hours_line_does_not_reduce_total_or_hide_pr06() -> None:
    roster = InMemoryRoster()
    roster.add(_member("Omar"))
    roster.add(_member("Sara", "22.00"))
    entries = [
        _entry("Omar", hours="-500.00", row=2),
        _entry("Sara", hours="10.00", row=3),
    ]
    lines = build_lines(entries, "MS-01", roster)
    assert lines[0].hours == D("-500.00")
    assert lines[0].gross == D("0.00")
    total = run_total(lines, excluded=[], denied=[])
    assert total == D("220.00")
    assert check_run(total, D("250.00"), POLICY).flags == ("PR-06",)


def test_run_total_skips_non_positive_gross_whatever_denied_says() -> None:
    line = DraftLine("x", None, "x", (), D("-1.00"), D("1.00"), D("-5.00"))
    ok = DraftLine("y", None, "y", (), D("1.00"), D("1.00"), D("7.00"))
    assert run_total([line, ok], [], []) == D("7.00")


def test_mixed_positive_and_negative_hours_zero_gross_and_deny() -> None:
    entries = [
        _entry(hours="40.00", row=2),
        _entry(hours="-38.00", day=date(2026, 11, 3), row=3),
    ]
    line = _line(_member("Omar"), entries)
    assert line.hours == D("2.00")
    assert line.gross == D("0.00")
    assert _check(_member("Omar"), entries) == (DENY, ("PR-02",))


@pytest.mark.parametrize("paid", ["ms-01", " MS-01", "Ms-01 "])
def test_pr07_normalises_milestone_casing_and_spaces(paid: str) -> None:
    hist = MemberHistory(PAID_BEFORE, frozenset({paid}))
    decision, flags = _check(_member("Omar"), [_entry()], hist)
    assert decision == DENY
    assert "PR-07" in flags


def test_pr07_normalises_requested_milestone() -> None:
    line = _line(_member("Omar"), [_entry()])
    hist = MemberHistory(PAID_BEFORE, frozenset({"MS-01"}))
    res = check_line(line, hist, " ms-01 ", POLICY, NOW)
    assert (res.decision, res.flags) == (DENY, ("PR-07",))


def test_build_lines_strips_milestone() -> None:
    roster = InMemoryRoster()
    roster.add(_member("Omar"))
    assert len(build_lines([_entry()], " ms-01 ", roster)) == 1


def test_cash_used_pct_clamps_negative_and_negative_cash() -> None:
    assert cash_used_pct(D("-5.00"), D("100.00")) == D("0.00")
    assert cash_used_pct(D("-5.00"), D("-100.00")) == D("0.00")
    assert cash_used_pct(D("5.00"), D("-100.00")) == D("999.99")
    assert cash_used_pct(D("0.00"), D("-100.00")) == D("0.00")


def test_check_line_requires_aware_now() -> None:
    line = _line(_member("Omar"), [_entry()])
    with pytest.raises(ValueError, match="timezone-aware"):
        check_line(line, HIST, "MS-01", POLICY, NOW.replace(tzinfo=None))


# ------------------------------------------------------- milestone canonical key


def test_milestone_key() -> None:
    assert milestone_key(" ms-01 ") == ("MS", 1)
    assert milestone_key("MS-1") == ("MS", 1)
    assert milestone_key("ms-12") == ("MS", 12)
    for bad in ("", "MS", "MS-", "-1", "MS-1A", "MS-١", "M1-2"):
        with pytest.raises(ValueError, match="milestone"):
            milestone_key(bad)


def test_build_lines_matches_zero_padded_milestone() -> None:
    roster = InMemoryRoster()
    roster.add(_member("Omar"))
    (line,) = build_lines([_entry(ms="MS-1")], "MS-01", roster)
    assert line.hours == D("4.00")
    assert len(build_lines([_entry(ms="MS-01")], "MS-1", roster)) == 1
    assert build_lines([_entry(ms="MS-2")], "MS-01", roster) == ()


def test_build_lines_unparseable_milestone_matches_nothing() -> None:
    roster = InMemoryRoster()
    roster.add(_member("Omar"))
    assert build_lines([_entry(ms="MS-01")], "garbage", roster) == ()
    assert build_lines([_entry(ms="garbage")], "garbage", roster) == ()


def test_pr07_matches_zero_padded_paid_milestone() -> None:
    hist = MemberHistory(PAID_BEFORE, frozenset({"MS-1"}))
    assert _check(_member("Omar"), [_entry()], hist) == (DENY, ("PR-07",))


def test_pr07_other_milestone_is_not_paid() -> None:
    hist = MemberHistory(PAID_BEFORE, frozenset({"MS-2"}))
    assert _check(_member("Omar"), [_entry()], hist) == (ALLOW, ())


def test_pr07_unparseable_codes_fall_back_to_string_comparison() -> None:
    line = _line(_member("Omar"), [_entry()])
    odd = MemberHistory(PAID_BEFORE, frozenset({"odd", "MS-01"}))
    res = check_line(line, odd, "MS-01", POLICY, NOW)
    assert (res.decision, res.flags) == (DENY, ("PR-07",))
    same = MemberHistory(PAID_BEFORE, frozenset({" Odd "}))
    assert check_line(line, same, "odd", POLICY, NOW).flags == ("PR-07",)
    other = MemberHistory(PAID_BEFORE, frozenset({"odd"}))
    assert check_line(line, other, "MS-01", POLICY, NOW).flags == ()


# ------------------------------------------------- PR-01 fallback and naive times


def test_pr01_falls_back_to_first_paid_at_when_never_paid_by_history() -> None:
    member = _member(
        "Omar",
        changed=NOW - timedelta(days=10),
        first_paid=NOW - timedelta(days=30),
    )
    hist = MemberHistory(last_paid_at=None, paid_milestones=frozenset())
    assert _check(member, [_entry()], hist) == (ASK, ("PR-01",))


def test_pr01_no_history_and_never_paid_outside_window_is_not_flagged_by_pr01() -> None:
    member = _member("Omar", changed=NOW - timedelta(days=10), first_paid=None)
    hist = MemberHistory(last_paid_at=None, paid_milestones=frozenset())
    assert _check(member, [_entry()], hist) == (ASK, ("PR-03",))


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2026, 11, 3)
    line = _line(_member("Omar", changed=naive), [_entry()])
    with pytest.raises(ValueError, match="email_changed_at must be timezone-aware"):
        check_line(line, HIST, "MS-01", POLICY, NOW)
    line = _line(_member("Omar", first_paid=naive), [_entry()])
    with pytest.raises(ValueError, match="first_paid_at must be timezone-aware"):
        check_line(line, HIST, "MS-01", POLICY, NOW)
    line = _line(_member("Omar"), [_entry()])
    bad = MemberHistory(last_paid_at=naive, paid_milestones=frozenset())
    with pytest.raises(ValueError, match="last_paid_at must be timezone-aware"):
        check_line(line, bad, "MS-01", POLICY, NOW)


def test_duplicates_are_detected_across_zero_padded_codes() -> None:
    entries = [_entry(ms="MS-1", row=2), _entry(ms="MS-01", row=3)]
    line = DraftLine(
        member_key="omar",
        member=_member("Omar"),
        display_name="Omar",
        entries=tuple(entries),
        hours=D("8.00"),
        rate=D("25.00"),
        gross=D("200.00"),
    )
    assert check_line(line, HIST, "MS-01", POLICY, NOW).flags == ("PR-05",)
    odd = replace(line, entries=(replace(entries[0], milestone_code="odd"),) * 2)
    assert check_line(odd, HIST, "MS-01", POLICY, NOW).flags == ("PR-05",)


@pytest.mark.parametrize(
    ("paid", "run_code", "flags"),
    [
        ({"MS-1", "odd"}, "MS-01", ("PR-07",)),
        ({"odd"}, "MS-01", ()),
        ({"odd"}, "odd", ("PR-07",)),
        ({"ms-01 "}, "MS-01", ("PR-07",)),
        ({"odd", "other"}, "MS-01", ()),
        ({"MS-1"}, "odd", ()),
    ],
)
def test_pr07_unparseable_paid_codes_only_add_matches(
    paid: set[str], run_code: str, flags: tuple[str, ...]
) -> None:
    line = _line(_member("Omar"), [_entry()])
    hist = MemberHistory(PAID_BEFORE, frozenset(paid))
    res = check_line(line, hist, run_code, POLICY, NOW)
    assert res.flags == flags
    assert res.decision == (DENY if flags else ALLOW)
