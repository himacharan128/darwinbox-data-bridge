"""Column profiles: what the model sees instead of the data.

Raw column values never reach the model. A profile is bounded at roughly 300 tokens
regardless of row count, which is what makes a 40-column x 500k-row export cost the
same as 40 x 50. The same profile feeds the deterministic evidence scorer, so it is
built once and serves both sides of the autonomy boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .provenance import SourceRef


class TypeCandidate(BaseModel):
    """How well a column's values parse as one type."""

    type: str
    parse_rate: float = Field(ge=0.0, le=1.0)
    formats: list[str] = Field(default_factory=list)  # date formats that parsed
    format_rates: dict[str, float] = Field(default_factory=dict)


class PatternMask(BaseModel):
    """A collapsed value shape, e.g. 'AAA-#####' at 0.98 coverage.

    The highest-information feature per token in the whole profile: 'EMP-#####'
    at 98% says more than fifty sample values.
    """

    mask: str
    coverage: float = Field(ge=0.0, le=1.0)


class ValueCount(BaseModel):
    value: str
    count: int


class ColumnProfile(BaseModel):
    source: SourceRef
    raw_name: str
    normalized_tokens: list[str]

    total: int
    non_null: int
    null_count: int
    null_markers_found: list[str] = Field(default_factory=list)

    distinct: int
    cardinality_ratio: float = Field(ge=0.0, le=1.0)

    type_candidates: list[TypeCandidate] = Field(default_factory=list)
    masks: list[PatternMask] = Field(default_factory=list)
    top_values: list[ValueCount] = Field(default_factory=list)
    sample: list[str] = Field(default_factory=list)

    length_min: int | None = None
    length_max: int | None = None
    length_mean: float | None = None

    def parse_rate(self, type_name: str) -> float:
        for c in self.type_candidates:
            if c.type == type_name:
                return c.parse_rate
        return 0.0

    def dominant_mask(self) -> PatternMask | None:
        return self.masks[0] if self.masks else None

    def for_model(self) -> dict:
        """The bounded view handed to the model. Never includes the full column.

        Note: this is the single chokepoint where source values reach the model,
        which is where field-level PII masking would be applied (see MASTER_PLAN A13).
        """
        return {
            "column": self.raw_name,
            "non_null": self.non_null,
            "total": self.total,
            "distinct": self.distinct,
            "cardinality_ratio": round(self.cardinality_ratio, 3),
            "null_markers": self.null_markers_found,
            "types": [
                {"type": c.type, "parse_rate": round(c.parse_rate, 3), "formats": c.formats}
                for c in self.type_candidates
                if c.parse_rate > 0
            ],
            "patterns": [{"mask": m.mask, "coverage": round(m.coverage, 3)} for m in self.masks],
            "top_values": [{"value": v.value, "count": v.count} for v in self.top_values],
            "sample": self.sample,
            "length": {"min": self.length_min, "max": self.length_max},
        }


class FileProfile(BaseModel):
    file: str
    sheet: str | None = None
    row_count: int
    columns: list[ColumnProfile]

    def by_name(self) -> dict[str, ColumnProfile]:
        return {c.raw_name: c for c in self.columns}
