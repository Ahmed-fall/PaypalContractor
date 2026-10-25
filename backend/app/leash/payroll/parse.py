"""CSV/XLSX timesheet reader: upload limits, formula-safe text, header signature.

This module only turns bytes into a raw table. It never interprets hours, dates
or money; later payroll steps map columns and build typed entries from it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from openpyxl import load_workbook  # type: ignore[import-untyped]
from openpyxl.utils.exceptions import (  # type: ignore[import-untyped]
    InvalidFileException,
)

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 500

_XLSX_MAGIC = b"PK\x03\x04"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_DELIMITERS = ",;\t"


class TimesheetError(Exception):
    """A timesheet upload was rejected. `message` is plain English for the UI."""

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class RawTable:
    """Headers plus non-blank data rows, with cells exactly as read."""

    headers: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


def escape_cell(text: str) -> str:
    """Prefix `'` to text a spreadsheet could execute as a formula."""
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def header_signature(headers: Sequence[str]) -> str:
    """sha256 over normalised headers (strip, collapse spaces, casefold)."""
    norm = [" ".join(h.split()).casefold() for h in headers]
    return hashlib.sha256("\x1f".join(norm).encode("utf-8")).hexdigest()


def file_hash(data: bytes) -> str:
    """sha256 of the raw upload bytes."""
    return hashlib.sha256(data).hexdigest()


def _is_blank(row: Iterable[object]) -> bool:
    return all(c is None or (isinstance(c, str) and not c.strip()) for c in row)


def _unsupported() -> TimesheetError:
    return TimesheetError("UNSUPPORTED_FILE_TYPE", "Upload a .csv or .xlsx timesheet.")


def _invalid(message: str) -> TimesheetError:
    return TimesheetError("TIMESHEET_INVALID", message)


def _read_csv_rows(data: bytes) -> list[list[object]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    sample = text[:4096]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        # Sniffer gives up on small or uniform files; use the header line.
        first = next((ln for ln in sample.splitlines() if ln.strip()), "")
        best = max(_DELIMITERS, key=first.count)
        if first.count(best) > first.count(","):
            delimiter = best
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return [[c if c != "" else None for c in row] for row in reader]


def _read_xlsx_rows(data: bytes) -> list[list[object]]:
    try:
        wb = load_workbook(
            io.BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
    except (zipfile.BadZipFile, KeyError, InvalidFileException, OSError) as exc:
        raise _unsupported() from exc
    try:
        ws = wb.worksheets[0] if wb.worksheets else None
        if ws is None:
            return []
        return [list(r) for r in ws.iter_rows(values_only=True)]
    except (zipfile.BadZipFile, KeyError) as exc:
        raise _unsupported() from exc
    finally:
        wb.close()


def read_table(data: bytes, filename: str) -> RawTable:
    """Read a CSV/XLSX upload. Checks run in order: size, type, parse."""
    if len(data) > MAX_BYTES:
        raise TimesheetError("FILE_TOO_LARGE", "This timesheet is larger than 2 MB.")

    name = filename.lower()
    if name.endswith(".xlsx"):
        if not data.startswith(_XLSX_MAGIC):
            raise _unsupported()
        raw = _read_xlsx_rows(data)
    elif name.endswith(".csv"):
        raw = _read_csv_rows(data)
    else:
        raise _unsupported()

    # Skip leading blank rows; the first non-blank row is the header.
    start = 0
    while start < len(raw) and _is_blank(raw[start]):
        start += 1
    if start == len(raw):
        raise _invalid("This timesheet is empty.")

    header_cells = raw[start]
    if any(c is None or not str(c).strip() for c in header_cells):
        raise _invalid(
            "This timesheet needs a header row with a name for every column."
        )
    headers = tuple(str(c).strip() for c in header_cells)
    width = len(headers)

    rows: list[tuple[object, ...]] = []
    for row in raw[start + 1 :]:
        if _is_blank(row):
            continue
        if len(rows) >= MAX_ROWS:
            raise _invalid("This timesheet has more than 500 rows.")
        cells = list(row[:width]) if len(row) > width else list(row)
        cells.extend([None] * (width - len(cells)))
        rows.append(tuple(cells))
    return RawTable(headers=headers, rows=tuple(rows))
