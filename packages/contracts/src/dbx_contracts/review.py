"""Escalations: the eleven ways autonomous processing is allowed to stop.

Each class carries its own evidence shape. A case that cannot say what evidence it
has, and what decision it needs, is not resolvable in one glance.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field

from .provenance import SourceRef


class EscalationClass(StrEnum):
    AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
    UNMAPPED_REQUIRED = "UNMAPPED_REQUIRED"
    AMBIGUOUS_VALUE = "AMBIGUOUS_VALUE"
    UNCERTAIN_IDENTITY = "UNCERTAIN_IDENTITY"
    CONFLICTING_FACTS = "CONFLICTING_FACTS"
    MISSING_REQUIRED = "MISSING_REQUIRED"
    VALIDATION_UNRESOLVED = "VALIDATION_UNRESOLVED"
    UNRESOLVED_REFERENCE = "UNRESOLVED_REFERENCE"
    LOW_CONFIDENCE_EXTRACTION = "LOW_CONFIDENCE_EXTRACTION"
    CROSS_RUN_COLLISION = "CROSS_RUN_COLLISION"
    DELIVERY_PERMANENT_FAILURE = "DELIVERY_PERMANENT_FAILURE"


class Action(StrEnum):
    APPROVE = "approve"
    CORRECT = "correct"
    REJECT = "reject"


class Option(BaseModel):
    """One choice a consultant can pick. Rendered as a button."""

    label: str
    value: str | None = None
    description: str | None = None
    recommended: bool = False


class CaseState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class ReviewCase(BaseModel):
    id: str
    run_id: str
    klass: EscalationClass

    # what it is about
    record_key: str | None = None  # natural key of the affected record, when record-level
    target_field: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)
    raw_values: list[str] = Field(default_factory=list)

    # why we stopped
    headline: str  # plain language, one line
    detail: str  # plain language, a short paragraph
    evidence: dict = Field(default_factory=dict)
    rule: str | None = None  # the constraint involved, when there is one
    attempts: list[str] = Field(default_factory=list)

    # what we need
    actions: list[Action] = Field(default_factory=list)
    options: list[Option] = Field(default_factory=list)

    state: CaseState = CaseState.OPEN
    blocks_records: list[str] = Field(default_factory=list)
    #: Records waiting only on THIS case being answered. They have nothing of their own
    #: to decide — a report whose manager is blocked is waiting on the manager — so they
    #: ride along rather than each becoming a question nobody can act on.
    child_records: list[str] = Field(default_factory=list)
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def waiting_count(self) -> int:
        """Everything this one answer would release."""
        return len({*self.blocks_records, *self.child_records})

    @property
    def has_proposal(self) -> bool:
        """Approval is only offered when there is something to approve."""
        return any(o.recommended for o in self.options)


class HumanDecision(BaseModel):
    case_id: str
    run_id: str
    action: Action
    value: str | None = None
    reason: str | None = None
    actor: str = "consultant"
    decided_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
