"""JSON and YAML inputs, and free text pasted into the browser.

Nested documents keep their path, so a value's provenance is `employees[3].contact.email`
rather than a row number that means nothing in a tree.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from dbx_contracts import ExtractedRecord, SourceRef

from .tabular import UnsupportedInput, record_id

#: Keys that commonly wrap a list of records at the top of an export.
_WRAPPERS = ("records", "data", "items", "rows", "results", "employees", "entities")


def _find_records(doc: Any) -> tuple[list[dict], str]:
    """Locate the list of record-shaped objects, and remember where it was found."""
    if isinstance(doc, list):
        return [d for d in doc if isinstance(d, dict)], "$"
    if isinstance(doc, dict):
        for key in _WRAPPERS:
            value = doc.get(key)
            if isinstance(value, list) and any(isinstance(v, dict) for v in value):
                return [v for v in value if isinstance(v, dict)], f"$.{key}"
        # A single object with no wrapper is one record.
        for key, value in doc.items():
            if isinstance(value, list) and any(isinstance(v, dict) for v in value):
                return [v for v in value if isinstance(v, dict)], f"$.{key}"
        return [doc], "$"
    raise UnsupportedInput("the document holds no record-shaped objects")


def flatten(node: Any, prefix: str = "") -> dict[str, str | None]:
    """Collapse a nested object to dotted keys so it can be profiled like a table."""
    out: dict[str, str | None] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        # A list of scalars becomes one joined value; a list of objects is not a field.
        if all(not isinstance(v, (dict, list)) for v in node):
            out[prefix] = ", ".join("" if v is None else str(v) for v in node)
    else:
        out[prefix] = None if node is None else str(node)
    return out


def read_structured(path: Path, *, display_name: str | None = None) -> list[ExtractedRecord]:
    name = display_name or path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise UnsupportedInput(f"{name}: could not be parsed ({exc})") from exc

    records, root = _find_records(doc)
    out: list[ExtractedRecord] = []
    for index, node in enumerate(records, start=1):
        values = flatten(node)
        if not values:
            continue
        out.append(
            ExtractedRecord(
                id=record_id(name, None, index),
                source=SourceRef(file=name, row=index, path=f"{root}[{index - 1}]"),
                values=values,
            )
        )
    return out


def read_pasted(text: str, *, display_name: str = "pasted text") -> list[ExtractedRecord]:
    """Turn pasted text into a synthetic source.

    Consultants paste a block out of a spreadsheet or an email far more often than
    they export a file, so the delimiter is sniffed rather than demanded.
    """
    import csv
    import io

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise UnsupportedInput("pasted text needs a header row and at least one record")

    sample = "\n".join(lines[:20])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in lines[0] else ","

    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    headers = [h.strip() or f"column_{i + 1}" for i, h in enumerate(rows[0])]
    out: list[ExtractedRecord] = []
    for index, row in enumerate(rows[1:], start=1):
        if not any(cell.strip() for cell in row):
            continue
        out.append(
            ExtractedRecord(
                id=record_id(display_name, None, index),
                source=SourceRef(file=display_name, row=index),
                values={headers[i]: (row[i] if i < len(row) else None)
                        for i in range(len(headers))},
            )
        )
    return out
