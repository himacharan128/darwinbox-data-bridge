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


class ColumnAssignment(BaseModel):
    """Where one column of a file belongs, decided alongside its neighbours."""

    source_column: str = Field(description="Exact column name from the provided list")
    target_field: str | None = Field(
        default=None,
        description="Exact field name from the schema, or null if it belongs to none",
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="0 = guessing, 1 = certain"
    )
    runner_up: str | None = Field(
        default=None,
        description="The next most plausible field, or null if nothing else fits",
    )
    reason: str = Field(description="One sentence citing the column name or its values")


class FileAssignment(BaseModel):
    """Every column of one file placed at once, so siblings inform each other."""

    assignments: list[ColumnAssignment] = Field(default_factory=list, max_length=60)


class FileRole(BaseModel):
    """What a file appears to be: the entity itself, or supporting lookup data."""

    file: str
    role: str = Field(description="One of: entity, lookup, unknown")
    lookup_name: str | None = None
    reason: str


class Finding(BaseModel):
    """What the agent worked out after going and looking at the data.

    It never applies anything. A finding becomes a recommended answer on the case
    that a person confirms, which is why `suggestion` has to be something the code
    can check before it is offered.
    """

    settled: bool = Field(
        description="true only if the data you looked at actually answers the question"
    )
    suggestion: str | None = Field(
        default=None,
        description=(
            "The value or field name you propose, copied exactly from what you saw. "
            "Null if nothing you found supports one."
        ),
    )
    conclusion: str = Field(
        description="One sentence a non-technical person can act on, citing what you found"
    )


class ProposedField(BaseModel):
    """One field the agent thinks the destination should have."""

    name: str = Field(description="snake_case field name")
    type: str = Field(description="one of: string, integer, number, boolean, date, email, enum")
    required: bool = Field(description="true only if every source row supplies it")
    unique: bool = False
    allowed: list[str] = Field(default_factory=list, description="for enum only")
    format: str | None = Field(default=None, description="for date only, e.g. YYYY-MM-DD")
    sources: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "The source columns this field came from, spelled exactly as they appear "
            "in the 'column' key of the profiles. Required: this is how the field is "
            "traced back to the data."
        ),
    )
    #: Filled in from the data after the model answers, not asked of the model.
    pattern: str | None = None
    reference: str | None = None
    reason: str = Field(description="why those columns are one field, in one sentence")


class ProposedSchema(BaseModel):
    """A destination shape inferred from the data, for a human to edit and approve."""

    entity: str = Field(description="what one record describes, singular and lowercase")
    fields: list[ProposedField] = Field(max_length=30)
