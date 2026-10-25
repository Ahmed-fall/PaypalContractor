"""Header-signature mapping cache around P2's AI column mapper.

A layout (the ordered set of headers) the owner has confirmed once is remembered by
its header signature, so later uploads of the same layout make no AI call at all.
For a new layout the AI mapper proposes a mapping; if it is missing, fails, times
out or returns nonsense, a plain header-name heuristic proposes one instead. Either
way the owner confirms before anything is stored. No money is computed here.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import ValidationError

from ...schemas.contracts import (
    ColumnMappingProposal,
    TimesheetField,
    TimesheetMappingConfirm,
)
from .parse import (
    RawTable,
    TimesheetError,
    escape_cell,
    header_signature,
    normalise_header,
)

Mapper = Callable[[list[str], list[list[str]]], Awaitable[ColumnMappingProposal]]

REQUIRED_FIELDS = frozenset({"member", "work_date", "hours", "milestone_code"})
SAMPLE_ROWS = 5
GUESS_CONFIDENCE = 0.6

_KEYWORDS: tuple[tuple[TimesheetField, tuple[str, ...]], ...] = (
    ("member", ("name", "employee", "member", "person", "staff")),
    ("work_date", ("date", "day", "work date")),
    ("hours", ("hours", "hrs", "hour", "time")),
    ("milestone_code", ("milestone", "project", "ms", "code")),
    ("rate", ("rate", "hourly rate")),
)


class MappingStore(Protocol):
    async def get(self, signature: str) -> dict[str, TimesheetField] | None: ...

    async def put(self, signature: str, mapping: dict[str, TimesheetField]) -> None: ...


class InMemoryMappingStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, TimesheetField]] = {}

    async def get(self, signature: str) -> dict[str, TimesheetField] | None:
        found = self._data.get(signature)
        return dict(found) if found is not None else None

    async def put(self, signature: str, mapping: dict[str, TimesheetField]) -> None:
        self._data[signature] = dict(mapping)


@dataclass(frozen=True)
class MappingResolution:
    signature: str
    known: bool
    mapping: dict[str, TimesheetField] | None
    proposal: ColumnMappingProposal | None


def _load_mapper() -> Mapper | None:
    """P2's ``map_timesheet_columns`` if ``..ai.leash`` exists, else None."""
    try:
        base = __name__.rsplit(".", 3)[0]  # backend.app
        name = f"{base}.ai.leash"
        if importlib.util.find_spec(name) is None:
            return None
        module = importlib.import_module(name)
        fn = getattr(module, "map_timesheet_columns", None)
        return fn if callable(fn) else None
    except Exception:  # noqa: BLE001 - a broken optional module means no mapper
        return None


def _norm(header: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", header.casefold()).split())


def guess_mapping(headers: Sequence[str]) -> ColumnMappingProposal:
    """Header-name heuristic: exact name match first, then any matching word."""
    normed = [_norm(h) for h in headers]
    taken: set[int] = set()
    mapping: dict[str, TimesheetField] = {}
    confidence: dict[str, float] = {}
    done: set[TimesheetField] = set()
    for exact in (True, False):
        for field, words in _KEYWORDS:
            if field in done:
                continue
            for i, n in enumerate(normed):
                if i in taken or not n or headers[i] in mapping:
                    continue
                hit = n in words if exact else bool(set(n.split()) & set(words))
                if hit:
                    taken.add(i)
                    done.add(field)
                    mapping[headers[i]] = field
                    confidence[headers[i]] = GUESS_CONFIDENCE
                    break
    return ColumnMappingProposal(mapping=mapping, confidence=confidence)


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, float):
        return str(Decimal(str(value)))
    if isinstance(value, str):
        return escape_cell(value)
    return str(value)


def _valid_proposal(
    proposal: object, headers: Sequence[str]
) -> ColumnMappingProposal | None:
    if not isinstance(proposal, ColumnMappingProposal):
        return None
    if not set(proposal.mapping) <= set(headers):
        return None
    return proposal


async def resolve_mapping(
    table: RawTable,
    store: MappingStore,
    mapper: Mapper | None,
    timeout_s: float = 8.0,
) -> MappingResolution:
    signature = header_signature(table.headers)
    known = await store.get(signature)
    if known is not None:
        return MappingResolution(signature, True, known, None)

    headers = list(table.headers)
    proposal: ColumnMappingProposal | None = None
    if mapper is not None:
        samples = [[_cell_text(c) for c in row] for row in table.rows[:SAMPLE_ROWS]]
        try:
            result = await asyncio.wait_for(mapper(headers, samples), timeout_s)
            proposal = _valid_proposal(result, headers)
        except Exception:  # noqa: BLE001 - AI is advisory only (incl. TimeoutError)
            proposal = None
    if proposal is None:
        proposal = guess_mapping(headers)
    return MappingResolution(signature, False, None, proposal)


async def confirm_mapping(
    store: MappingStore,
    signature: str,
    mapping: dict[str, str],
    headers: Sequence[str],
) -> dict[str, TimesheetField]:
    """Validate and remember a mapping; every key must be one of `headers`."""
    try:
        confirmed = TimesheetMappingConfirm.model_validate({"mapping": mapping})
    except ValidationError as exc:
        raise TimesheetError(
            "TIMESHEET_INVALID",
            "Each column may be mapped to one field, and only to known fields.",
        ) from exc
    clean = dict(confirmed.mapping)
    known = {normalise_header(h) for h in headers}
    keys = [normalise_header(key) for key in clean]
    if len(set(keys)) != len(keys) or any(key not in known for key in keys):
        raise TimesheetError(
            "TIMESHEET_INVALID", "The column mapping does not match this timesheet."
        )
    missing = sorted(REQUIRED_FIELDS - set(clean.values()))
    if missing:
        raise TimesheetError(
            "TIMESHEET_INVALID",
            "Map a column to every required field: " + ", ".join(missing) + ".",
            {"missing": missing},
        )
    await store.put(signature, clean)
    return clean
