"""Extracted and canonical records, and what validation says about them."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .provenance import FieldProvenance, SourceRef


class ExtractedRecord(BaseModel):
    """One row as read from one input, before any interpretation."""

    id: str
    source: SourceRef
    values: dict[str, str | None]  # raw header -> raw cell text

    def get(self, column: str) -> str | None:
        return self.values.get(column)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    target_field: str | None
    rule: str
    message: str
    severity: Severity = Severity.ERROR
    value: str | None = None


class ValidationResult(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]


class RecordState(StrEnum):
    PROCESSING = "processing"
    READY = "ready"
    BLOCKED = "blocked"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXCLUDED = "excluded"
    ROLLED_BACK = "rolled_back"


class CanonicalRecord(BaseModel):
    """One reconciled entity shaped to the approved target schema.

    `provenance` carries a per-field trail back to the contributing cells, which is
    what lets a review card show evidence without anyone opening a source file.
    """

    id: str
    run_id: str
    natural_key: str | None = None
    values: dict[str, object] = Field(default_factory=dict)
    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)
    contributing: list[SourceRef] = Field(default_factory=list)

    state: RecordState = RecordState.PROCESSING
    validation: ValidationResult = Field(default_factory=ValidationResult)
    open_cases: list[str] = Field(default_factory=list)
    validation_attempts: int = 0

    @property
    def deliverable(self) -> bool:
        """A record with any open case is never deliverable (forbidden action FBD-008)."""
        return self.state is RecordState.READY and not self.open_cases and self.validation.ok
