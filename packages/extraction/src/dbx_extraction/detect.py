"""Identify what a file actually is, not what it claims to be.

TD003: verify the file signature after upload rather than trusting the extension or
the browser's MIME type. A .csv that is really a zip, or a .xlsx that is really HTML,
should be reported clearly rather than exploding inside a parser.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Encodings tried in order. utf-8-sig first so a BOM does not become part of a header.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # legacy .xls
_PDF_MAGIC = b"%PDF"


class Kind(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"
    XLS_LEGACY = "xls_legacy"
    PDF = "pdf"
    JSON = "json"
    YAML = "yaml"
    UNKNOWN = "unknown"
    EMPTY = "empty"
    ENCRYPTED = "encrypted"


@dataclass(frozen=True)
class Detected:
    kind: Kind
    encoding: str | None = None
    delimiter: str | None = None
    note: str | None = None

    @property
    def supported(self) -> bool:
        return self.kind in {Kind.CSV, Kind.XLSX}


def sniff(path: Path) -> Detected:
    if not path.exists() or path.stat().st_size == 0:
        return Detected(Kind.EMPTY, note="file is empty")

    head = path.read_bytes()[:2048]

    if head.startswith(_ZIP_MAGIC):
        # xlsx is a zip; an encrypted office file is an OLE container instead
        return Detected(Kind.XLSX)
    if head.startswith(_OLE_MAGIC):
        return Detected(
            Kind.XLS_LEGACY,
            note="legacy .xls (or an encrypted workbook) — outside the supported set",
        )
    if head.startswith(_PDF_MAGIC):
        return Detected(Kind.PDF, note="PDF extraction arrives in Phase 4")

    encoding = _encoding_for(path)
    if encoding is None:
        return Detected(Kind.UNKNOWN, note="could not decode as text in any known encoding")

    text = path.read_text(encoding=encoding, errors="replace")
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return Detected(Kind.JSON, encoding=encoding, note="JSON ingestion arrives in Phase 4")
    if stripped.startswith("---"):
        return Detected(Kind.YAML, encoding=encoding, note="YAML ingestion arrives in Phase 4")

    return Detected(Kind.CSV, encoding=encoding, delimiter=_delimiter_for(text))


def _encoding_for(path: Path) -> str | None:
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            raw.decode(enc)
        except UnicodeDecodeError:
            continue
        return enc
    return None


def _delimiter_for(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Sniffer gives up on single-column files; comma is the safe default.
        return ","
