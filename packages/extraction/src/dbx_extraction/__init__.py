"""Deterministic extraction with cell-level provenance."""
from .detect import Detected, Kind, is_sidecar, sniff
from .documents import Cell, OcrEngine, Word, confidence_for, crop, read_pages, read_pdf
from .structured import flatten, read_pasted, read_structured
from .tabular import UnsupportedInput, read, record_id

__all__ = [
    "Cell",
    "Detected",
    "Kind",
    "OcrEngine",
    "UnsupportedInput",
    "Word",
    "confidence_for",
    "crop",
    "flatten",
    "is_sidecar",
    "read",
    "read_pages",
    "read_pasted",
    "read_pdf",
    "read_structured",
    "record_id",
    "sniff",
]
