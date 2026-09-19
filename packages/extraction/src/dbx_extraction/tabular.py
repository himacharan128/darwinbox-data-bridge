"""CSV and XLSX readers that keep every cell's address.

Provenance is not decoration here: an escalation card renders the source location,
so a reader that loses the row number makes the case unresolvable at a glance.
"""

from __future__ import annotations

import csv
from pathlib import Path

from dbx_contracts import ExtractedRecord, SourceRef
from openpyxl import load_workbook

from .detect import Detected, Kind, sniff


class UnsupportedInput(Exception):
    """Raised with a consultant-readable reason, never a stack trace."""


def read(path: Path, *, display_name: str | None = None) -> list[ExtractedRecord]:
    """Read one supported input into records, preserving where each value came from."""
    detected = sniff(path)
    name = display_name or path.name
    if not detected.supported:
        raise UnsupportedInput(detected.note or f"{name}: unsupported file type ({detected.kind})")
    if detected.kind is Kind.CSV:
        return _read_csv(path, name, detected)
    return _read_xlsx(path, name)


def record_id(file: str, sheet: str | None, row: int) -> str:
    """Identity derived from the cell address, never random.

    A uuid4 here makes every replay produce different ids, which quietly reorders
    anything that tie-breaks on identity — record matching did, so a run's outcome
    drifted between replays. Deriving it from the source address also means a record
    keeps the same identity across a resume, which is what an audit trail needs.
    """
    return f"{file}#{sheet or ''}#{row}"


def _clean_header(raw: object, index: int) -> str:
    text = "" if raw is None else str(raw).strip()
    return text or f"column_{index + 1}"


def _read_csv(path: Path, name: str, detected: Detected) -> list[ExtractedRecord]:
    with path.open(encoding=detected.encoding or "utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=detected.delimiter or ","))
    if not rows:
        return []

    headers = [_clean_header(h, i) for i, h in enumerate(rows[0])]
    out: list[ExtractedRecord] = []
    for line_no, row in enumerate(rows[1:], start=1):
        if not any((c or "").strip() for c in row):
            continue  # blank line, not a record
        values = {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
        out.append(
            ExtractedRecord(
                id=record_id(name, None, line_no),
                source=SourceRef(file=name, row=line_no),
                values=values,
            )
        )
    return out


def _read_xlsx(path: Path, name: str) -> list[ExtractedRecord]:
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises a wide variety here
        raise UnsupportedInput(f"{name}: workbook could not be opened ({exc})") from exc

    out: list[ExtractedRecord] = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            continue  # empty sheet
        headers = [_clean_header(h, i) for i, h in enumerate(header_row)]
        if not any(str(h or "").strip() for h in header_row):
            continue

        for line_no, row in enumerate(rows, start=1):
            if not any(c is not None and str(c).strip() for c in row):
                continue
            values = {
                headers[i]: (_cell(row[i]) if i < len(row) else None) for i in range(len(headers))
            }
            out.append(
                ExtractedRecord(
                    id=record_id(name, ws.title, line_no),
                    source=SourceRef(file=name, sheet=ws.title, row=line_no),
                    values=values,
                )
            )
    wb.close()
    return out


def _cell(value: object) -> str | None:
    """Excel hands back typed values; keep them as text and let the profiler decide.

    Dates are the case that matters: openpyxl returns datetime objects, and rendering
    them with str() would invent a format the source never had.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        return text[:10] if text.endswith("T00:00:00") else text
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
