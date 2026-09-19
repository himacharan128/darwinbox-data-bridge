"""The target schema language.

This is a schema *language*, not an HR schema. Nothing here knows what an employee
is. A `MigrationSchema` is whatever the user supplies or approves at runtime; the
fixture under tests/ is one instance of it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

# User-facing date tokens -> strftime. Users write YYYY-MM-DD, not %Y-%m-%d.
_DATE_TOKENS = [
    ("YYYY", "%Y"),
    ("MM", "%m"),
    ("DD", "%d"),
    ("HH", "%H"),
    ("mm", "%M"),
    ("SS", "%S"),
]


def to_strftime(fmt: str) -> str:
    out = fmt
    for token, code in _DATE_TOKENS:
        out = out.replace(token, code)
    return out


class FieldType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    EMAIL = "email"
    ENUM = "enum"


class FieldSpec(BaseModel):
    """One target field and the constraints that make a mapping checkable.

    Every constraint here is a measurable signal for the evidence scorer, which is
    why the scorer generalises: it reads constraints, never field names.
    """

    name: str
    type: FieldType
    required: bool = False
    unique: bool = False
    nullable: bool = True

    # string-ish
    pattern: str | None = None
    case_sensitive: bool = False
    max_length: int | None = None
    min_length: int | None = None

    # numeric
    minimum: float | None = None
    maximum: float | None = None

    # enum
    allowed: list[str] | None = None

    # temporal
    format: str | None = None

    # relational
    reference: str | None = None  # "<lookup>.<key>" or "<entity>.<field>"

    aliases: list[str] = Field(default_factory=list)
    description: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.type is FieldType.ENUM and not self.allowed:
            raise ValueError(f"field {self.name!r}: enum requires 'allowed'")
        if self.pattern:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"field {self.name!r}: bad pattern: {exc}") from exc
        if self.reference and "." not in self.reference:
            raise ValueError(f"field {self.name!r}: reference must be '<target>.<key>'")
        if self.required and self.nullable is False and self.type is FieldType.ENUM:
            pass  # allowed; kept explicit for readability
        return self

    @property
    def strftime(self) -> str | None:
        return to_strftime(self.format) if self.format else None

    @property
    def reference_target(self) -> tuple[str, str] | None:
        if not self.reference:
            return None
        target, _, key = self.reference.partition(".")
        return target, key

    def match_names(self) -> list[str]:
        """Names a source header could plausibly be compared against."""
        return [self.name, *self.aliases]


class LookupSpec(BaseModel):
    name: str
    key: str
    fields: list[str]

    @model_validator(mode="after")
    def _key_present(self) -> Self:
        if self.key not in self.fields:
            raise ValueError(f"lookup {self.name!r}: key {self.key!r} not among fields")
        return self


class MigrationSchema(BaseModel):
    schema_version: int = 1
    entity: str
    description: str | None = None
    fields: list[FieldSpec]
    lookups: list[LookupSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        names = [f.name for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate field names: {sorted(dupes)}")

        known = {lk.name for lk in self.lookups} | {self.entity}
        for f in self.fields:
            if ref := f.reference_target:
                target, key = ref
                if target not in known:
                    raise ValueError(
                        f"field {f.name!r}: reference target {target!r} is not a lookup or the entity"
                    )
                if target == self.entity and key not in names:
                    raise ValueError(f"field {f.name!r}: self-reference key {key!r} is not a field")
        return self

    @property
    def by_name(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}

    @property
    def required_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.required]

    def to_json_schema(self) -> dict[str, Any]:
        """Compile to JSON Schema (TD002). Used for contract docs and payload checks."""
        props: dict[str, Any] = {}
        for f in self.fields:
            node: dict[str, Any] = {}
            match f.type:
                case FieldType.INTEGER:
                    node["type"] = "integer"
                case FieldType.NUMBER:
                    node["type"] = "number"
                case FieldType.BOOLEAN:
                    node["type"] = "boolean"
                case FieldType.ENUM:
                    node["type"] = "string"
                    node["enum"] = f.allowed
                case FieldType.EMAIL:
                    node["type"] = "string"
                    node["format"] = "email"
                case FieldType.DATE:
                    node["type"] = "string"
                    node["format"] = "date"
                case FieldType.DATETIME:
                    node["type"] = "string"
                    node["format"] = "date-time"
                case _:
                    node["type"] = "string"
            if f.pattern:
                node["pattern"] = f.pattern
            if f.max_length is not None:
                node["maxLength"] = f.max_length
            if f.min_length is not None:
                node["minLength"] = f.min_length
            if f.minimum is not None:
                node["minimum"] = f.minimum
            if f.maximum is not None:
                node["maximum"] = f.maximum
            if f.nullable and not f.required:
                node = {"anyOf": [node, {"type": "null"}]}
            if f.description:
                node["description"] = f.description
            props[f.name] = node
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": self.entity,
            "type": "object",
            "properties": props,
            "required": [f.name for f in self.required_fields],
            "additionalProperties": False,
        }
