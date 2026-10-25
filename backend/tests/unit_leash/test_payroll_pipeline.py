"""End-to-end demo payroll: CSV -> table -> mapping -> entries -> lines -> checks."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.leash.payroll.calculate import (
    build_lines,
    cash_used_pct,
    run_total,
)
from backend.app.leash.payroll.checks import MemberHistory, check_line, check_run
from backend.app.leash.payroll.mapping import (
    InMemoryMappingStore,
    Mapper,
    confirm_mapping,
    resolve_mapping,
)
from backend.app.leash.payroll.parse import read_table
from backend.app.leash.payroll.timesheet import parse_entries
from backend.app.leash.policy import default_policy
from backend.app.leash.roster import InMemoryRoster, create_member, update_member
from backend.app.schemas.contracts import (
    ColumnMappingProposal,
    LeashDecision,
    TeamMemberCreate,
    TeamMemberUpdate,
)

D = Decimal
NOW = datetime(2026, 11, 4, 18, 40, tzinfo=UTC)
PAID = datetime(2026, 9, 30, 12, tzinfo=UTC)
CASH_IN = D("850.00")
HEADER = b"Employee,Day,Hrs,Project"
BODY = (
    b"\nOmar,2026-11-02,4,MS-01"
    b"\nOmar,2026-11-03,4,MS-01"
    b"\nOmar,2026-11-04,4,MS-01"
    b"\nSara,2026-11-02,4,MS-01"
    b"\nSara,2026-11-03,3,MS-01"
    b"\nSara,2026-11-04,3,MS-01"
    b"\nYoussef,2026-11-02,3,MS-01"
    b"\nYoussef,2026-11-03,3,MS-01"
    b"\nYoussef,2026-11-04,2,MS-01\n"
)
CSV = HEADER + BODY
CSV_VARIANT = b"employee,DAY,hrs ,Project" + BODY


def _roster() -> InMemoryRoster:
    roster = InMemoryRoster()
    people = [
        ("Omar", "sb-omar@personal.example.com", "25.00"),
        ("Sara", "sb-sara@personal.example.com", "22.00"),
        ("Youssef", "sb-youssef@personal.example.com", "25.00"),
    ]
    for name, email, rate in people:
        member = create_member(
            TeamMemberCreate(name=name, payout_email=email, hourly_rate=D(rate)),
            NOW - timedelta(days=60),
        )
        member = replace(member, first_paid_at=PAID)
        if name == "Omar":
            member = update_member(
                member,
                TeamMemberUpdate(payout_email="sb-omar.new@personal.example.com"),
                NOW - timedelta(days=1),
            )
        roster.add(member)
    return roster


@pytest.mark.asyncio
async def test_demo_pipeline_end_to_end() -> None:
    policy = default_policy()
    history = MemberHistory(last_paid_at=PAID, paid_milestones=frozenset())
    roster = _roster()
    store = InMemoryMappingStore()
    calls: list[int] = []

    async def mapper(
        headers: list[str], samples: list[list[str]]
    ) -> ColumnMappingProposal:
        calls.append(1)
        raise AssertionError("a known layout must not call the mapper")

    forbidden: Mapper = mapper

    # 1-5: first upload, new layout, owner confirms the heuristic proposal
    table = read_table(CSV, "demo.csv")
    assert len(table.rows) == 9
    first = await resolve_mapping(table, store, None)
    assert first.known is False
    assert first.proposal is not None
    mapping = await confirm_mapping(
        store, first.signature, dict(first.proposal.mapping), table.headers
    )
    entries = parse_entries(table, mapping)
    assert len(entries) == 9

    # 6-8: lines, checks, run
    lines = build_lines(entries, "MS-01", roster)
    assert [ln.gross for ln in lines] == [D("300.00"), D("220.00"), D("200.00")]
    results = [check_line(ln, history, "MS-01", policy, NOW) for ln in lines]
    assert [(r.decision, r.flags) for r in results] == [
        (LeashDecision.ASK, ("PR-01",)),
        (LeashDecision.ALLOW, ()),
        (LeashDecision.ALLOW, ()),
    ]
    denied = [r.member_key for r in results if r.decision is LeashDecision.DENY]
    total = run_total(lines, excluded=[], denied=denied)
    assert total == D("720.00")
    assert cash_used_pct(total, CASH_IN) == D("84.71")
    run = check_run(total, CASH_IN, policy)
    assert (run.decision, run.flags) == (LeashDecision.ASK, ("PR-06",))

    omar_key = lines[0].member_key
    total2 = run_total(lines, excluded=[omar_key], denied=denied)
    assert total2 == D("420.00")
    assert cash_used_pct(total2, CASH_IN) == D("49.41")
    run2 = check_run(total2, CASH_IN, policy)
    assert (run2.decision, run2.flags) == (LeashDecision.ALLOW, ())

    # Re-upload with differently written headers: known layout, no mapper call
    table2 = read_table(CSV_VARIANT, "demo-again.csv")
    assert table2.headers != table.headers
    second = await resolve_mapping(table2, store, forbidden)
    assert second.known is True
    assert second.signature == first.signature
    assert calls == []
    assert second.mapping is not None
    entries2 = parse_entries(table2, second.mapping)
    lines2 = build_lines(entries2, "MS-01", roster)
    assert [(ln.member_key, ln.hours, ln.gross) for ln in lines2] == [
        (ln.member_key, ln.hours, ln.gross) for ln in lines
    ]
