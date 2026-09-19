"""Where a value came from.

Provenance is what makes an escalation resolvable in one glance: the review card
shows the original value and the exact cell it came from, so nobody has to open
the source file.
"""

from __future__ import annotations

from pydantic import BaseModel


class SourceRef(BaseModel, frozen=True):
    """A location inside a submitted input. Fields not applicable stay None."""

    file: str
    sheet: str | None = None
    row: int | None = None  # 1-based, data rows (header excluded)
    column: str | None = None  # raw header text
    page: int | None = None  # PDF
    path: str | None = None  # JSON/YAML dotted path

    def label(self) -> str:
        bits = [self.file]
        if self.sheet:
            bits.append(f"sheet {self.sheet}")
        if self.page is not None:
            bits.append(f"page {self.page}")
        if self.row is not None:
            bits.append(f"row {self.row}")
        if self.column:
            bits.append(f"column {self.column!r}")
        if self.path:
            bits.append(self.path)
        return " · ".join(bits)


class Transformation(BaseModel):
    """One applied cleanup step, kept so before/after is always inspectable."""

    rule: str  # e.g. "trim_boundary_whitespace"
    before: str | None
    after: str | None
    reason: str


class FieldProvenance(BaseModel):
    """How one target field on one canonical record got its value."""

    target_field: str
    source: SourceRef
    raw_value: str | None
    value: object = None
    transformations: list[Transformation] = []

    @property
    def was_transformed(self) -> bool:
        return bool(self.transformations)
