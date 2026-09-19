"""Check records against the approved schema.

Every rule reads a declared constraint. Validation is deliberately separate from
cleanup: cleanup proposes, validation disposes, and the gap between them is where the
bounded correct-then-revalidate cycle lives.
"""

from __future__ import annotations

import re
from collections import defaultdict

from dbx_contracts import (
    FieldSpec,
    FieldType,
    MigrationSchema,
    Severity,
    ValidationIssue,
    ValidationResult,
)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def validate_record(
    values: dict[str, object],
    schema: MigrationSchema,
    *,
    lookups: dict[str, set[str]] | None = None,
    known_keys: dict[str, set[str]] | None = None,
) -> ValidationResult:
    lookups = lookups or {}
    issues: list[ValidationIssue] = []

    for field in schema.fields:
        value = values.get(field.name)

        if value is None or value == "":
            if field.required:
                issues.append(
                    ValidationIssue(
                        target_field=field.name,
                        rule="required",
                        message=f"{field.name} is required and no source supplied a value",
                    )
                )
            continue

        issues.extend(_check_type(field, value))
        issues.extend(_check_constraints(field, value))
        issues.extend(_check_reference(field, value, values, schema, lookups, known_keys or {}))

    return ValidationResult(issues=issues)


def _check_type(field: FieldSpec, value: object) -> list[ValidationIssue]:
    text = str(value)
    match field.type:
        case FieldType.EMAIL if not _EMAIL.match(text):
            return [
                ValidationIssue(
                    target_field=field.name,
                    rule="type:email",
                    value=text,
                    message=f"{text!r} is not a valid email address",
                )
            ]
        case FieldType.INTEGER if not isinstance(value, int):
            return [
                ValidationIssue(
                    target_field=field.name,
                    rule="type:integer",
                    value=text,
                    message=f"{text!r} is not a whole number",
                )
            ]
        case FieldType.NUMBER if not isinstance(value, (int, float)):
            return [
                ValidationIssue(
                    target_field=field.name,
                    rule="type:number",
                    value=text,
                    message=f"{text!r} is not a number",
                )
            ]
        case FieldType.ENUM if field.allowed and text not in field.allowed:
            return [
                ValidationIssue(
                    target_field=field.name,
                    rule="type:enum",
                    value=text,
                    message=f"{text!r} is not one of: {', '.join(field.allowed)}",
                )
            ]
        case _:
            return []
    return []


def _check_constraints(field: FieldSpec, value: object) -> list[ValidationIssue]:
    out: list[ValidationIssue] = []
    text = str(value)

    if field.pattern and not re.match(field.pattern, text):
        out.append(
            ValidationIssue(
                target_field=field.name,
                rule="pattern",
                value=text,
                message=f"{text!r} does not match the required format {field.pattern}",
            )
        )
    if field.max_length is not None and len(text) > field.max_length:
        out.append(
            ValidationIssue(
                target_field=field.name,
                rule="max_length",
                value=text,
                message=f"{field.name} allows {field.max_length} characters; this has {len(text)}",
            )
        )
    if field.min_length is not None and len(text) < field.min_length:
        out.append(
            ValidationIssue(
                target_field=field.name,
                rule="min_length",
                value=text,
                message=f"{field.name} needs at least {field.min_length} characters",
            )
        )
    if isinstance(value, (int, float)):
        if field.minimum is not None and value < field.minimum:
            out.append(
                ValidationIssue(
                    target_field=field.name,
                    rule="minimum",
                    value=text,
                    message=f"{field.name} must be at least {field.minimum}",
                )
            )
        if field.maximum is not None and value > field.maximum:
            out.append(
                ValidationIssue(
                    target_field=field.name,
                    rule="maximum",
                    value=text,
                    message=f"{field.name} must be at most {field.maximum}",
                )
            )
    return out


def _check_reference(
    field: FieldSpec,
    value: object,
    record: dict[str, object],
    schema: MigrationSchema,
    lookups: dict[str, set[str]],
    known_keys: dict[str, set[str]],
) -> list[ValidationIssue]:
    ref = field.reference_target
    if not ref:
        return []
    target, key = ref
    text = str(value)

    if target == schema.entity:
        if record.get(key) == value:
            return [
                ValidationIssue(
                    target_field=field.name,
                    rule="reference:self",
                    value=text,
                    message=f"{field.name} points at the record's own {key}",
                )
            ]
        universe = known_keys.get(target)
        if universe is not None and text not in universe:
            return [
                ValidationIssue(
                    target_field=field.name,
                    rule="reference:missing",
                    value=text,
                    message=f"no record in this run has {key} {text!r}",
                )
            ]
        return []

    universe = lookups.get(target)
    if universe is not None and text not in universe:
        return [
            ValidationIssue(
                target_field=field.name,
                rule="reference:missing",
                value=text,
                message=f"{text!r} is not a known {target}",
            )
        ]
    return []


def check_uniqueness(
    records: list[tuple[str, dict[str, object]]], schema: MigrationSchema
) -> dict[str, list[ValidationIssue]]:
    """Dataset-wide uniqueness. Per-record checks cannot see a collision."""
    out: dict[str, list[ValidationIssue]] = defaultdict(list)
    for field in schema.fields:
        if not field.unique:
            continue
        seen: dict[str, list[str]] = defaultdict(list)
        for record_id, values in records:
            value = values.get(field.name)
            if value is None or value == "":
                continue
            key = str(value) if field.case_sensitive else str(value).casefold()
            seen[key].append(record_id)
        for key, ids in seen.items():
            if len(ids) > 1:
                for record_id in ids:
                    others = [i for i in ids if i != record_id]
                    out[record_id].append(
                        ValidationIssue(
                            target_field=field.name,
                            rule="unique",
                            value=key,
                            severity=Severity.ERROR,
                            message=(
                                f"{field.name} must be unique but {len(ids)} records share "
                                f"this value ({', '.join(others[:3])})"
                            ),
                        )
                    )
    return out
