"""Deterministic extraction with cell-level provenance."""

from .detect import Detected, Kind, sniff
from .tabular import UnsupportedInput, read, record_id

__all__ = ["Detected", "Kind", "UnsupportedInput", "read", "record_id", "sniff"]
