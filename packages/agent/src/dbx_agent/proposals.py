"""Typed proposals the model is allowed to make.

The model never decides anything. It nominates candidates; deterministic code scores
them and a policy gate makes the call. These schemas are what the model's forced tool
call must conform to.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FieldVote(BaseModel):
    """One semantic judgement about a (column, field) pair."""

    target_field: str = Field(description="Exact target field name from the provided schema")
    vote: float = Field(ge=0.0, le=1.0, description="0 = unrelated, 1 = certainly this field")
    reason: str = Field(description="One sentence citing the column name or sample values")


class MappingVote(BaseModel):
    """The model's ranked opinion for one source column."""

    source_column: str
    votes: list[FieldVote] = Field(default_factory=list, max_length=5)


class FileRole(BaseModel):
    """What a file appears to be: the entity itself, or supporting lookup data."""

    file: str
    role: str = Field(description="One of: entity, lookup, unknown")
    lookup_name: str | None = None
    reason: str


class InvestigationStep(BaseModel):
    tool: str
    argument: str
    finding: str


class InvestigationResult(BaseModel):
    """What the bounded investigator learned before giving up or deciding."""

    resolved: bool
    chosen_field: str | None = None
    steps: list[InvestigationStep] = Field(default_factory=list)
    conclusion: str


class ProposedField(BaseModel):
    """One field the agent thinks the destination should have."""

    name: str = Field(description="snake_case field name")
    type: str = Field(description="one of: string, integer, number, boolean, date, email, enum")
    required: bool = Field(description="true only if every source row supplies it")
    unique: bool = False
    allowed: list[str] = Field(default_factory=list, description="for enum only")
    format: str | None = Field(default=None, description="for date only, e.g. YYYY-MM-DD")
    reason: str = Field(description="which source columns led to this, in one sentence")


class ProposedSchema(BaseModel):
    """A destination shape inferred from the data, for a human to edit and approve."""

    entity: str = Field(description="what one record describes, singular and lowercase")
    fields: list[ProposedField] = Field(max_length=30)
