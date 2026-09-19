"""The safe-transformation whitelist.

Exhaustive by design: anything not listed here escalates rather than being guessed.
Every rule is driven by a constraint the target field declares, so none of them knows
what any particular field means.

The line is "reversible and evidence-backed" versus "inventing data". Stripping
punctuation so a value satisfies a declared pattern is safe. Prefixing a country code
so it satisfies that pattern is not, however obvious it looks.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dc_field

from dbx_contracts import ColumnProfile, FieldSpec, FieldType, Transformation

from .profiling import NULL_MARKERS, decidable_date_formats

#: Punctuation that may be removed to satisfy a declared pattern.
_PATTERN_NOISE = re.compile(r"[\s\-().]")
_WS_RUN = re.compile(r"\s{2,}")


class UnsafeValue(Exception):
    """The value cannot be cleaned safely. Carries the reason a human will read."""

    def __init__(self, reason: str, *, alternatives: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.alternatives = alternatives or []


@dataclass
class ColumnPlan:
    """Column-level decisions that individual values cannot make for themselves.

    Date format is the case that matters. A single value like 03/04/2024 is
    undecidable; the column it sits in may or may not contain a value above the
    twelfth that settles it for everyone.
    """

    date_format: str | None = None
    date_ambiguous_between: list[str] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)


def plan_column(profile: ColumnProfile, field: FieldSpec) -> ColumnPlan:
    """Decide once per column what can be applied to every value in it."""
    if field.type not in (FieldType.DATE, FieldType.DATETIME):
        return ColumnPlan()

    values = [vc.value for vc in profile.top_values] or profile.sample
    rates, decidable = decidable_date_formats(values)
    if not rates:
        return ColumnPlan()

    best = max(rates.values())
    winners = [f for f, r in rates.items() if r >= best - 1e-9]

    if len(winners) == 1:
        return ColumnPlan(date_format=winners[0], notes=[f"{winners[0]} parses {best:.0%}"])

    settled = [f for f in winners if decidable.get(f)]
    if len(settled) == 1:
        return ColumnPlan(
            date_format=settled[0],
            notes=[
                (
                    f"{settled[0]} is the only reading consistent with every value "
                    "(some day components exceed 12)"
                )
            ],
        )

    return ColumnPlan(
        date_ambiguous_between=sorted(winners),
        notes=[
            (
                f"{len(winners)} formats parse every value and none is ruled out; "
                "no value in the column distinguishes them"
            )
        ],
    )


def clean(
    raw: str | None, field: FieldSpec, plan: ColumnPlan | None = None
) -> tuple[object, list[Transformation]]:
    """Apply every safe rule that applies. Raises UnsafeValue rather than guessing."""
    plan = plan or ColumnPlan()
    steps: list[Transformation] = []

    if raw is None:
        return None, steps

    value = raw

    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        steps.append(
            Transformation(
                rule="unicode_nfc",
                before=value,
                after=normalized,
                reason="normalised to Unicode NFC",
            )
        )
        value = normalized

    trimmed = value.strip()
    if trimmed != value:
        steps.append(
            Transformation(
                rule="trim_boundary_whitespace",
                before=value,
                after=trimmed,
                reason="removed leading/trailing whitespace",
            )
        )
        value = trimmed

    collapsed = _WS_RUN.sub(" ", value)
    if collapsed != value:
        steps.append(
            Transformation(
                rule="collapse_repeated_whitespace",
                before=value,
                after=collapsed,
                reason="collapsed repeated spaces",
            )
        )
        value = collapsed

    if value.lower() in NULL_MARKERS:
        steps.append(
            Transformation(
                rule="null_marker", before=value, after=None, reason=f"{value!r} means absent"
            )
        )
        return None, steps

    # Identifiers are never reshaped. Case and padding can carry meaning.
    if field.case_sensitive:
        _require_pattern(value, field)
        return value, steps

    match field.type:
        case FieldType.EMAIL:
            value = _clean_email(value, steps)
        case FieldType.ENUM:
            value = _clean_enum(value, field, steps)
        case FieldType.DATE | FieldType.DATETIME:
            return _clean_date(value, field, plan, steps), steps
        case FieldType.INTEGER:
            value = _clean_number(value, steps, integer=True)
        case FieldType.NUMBER:
            value = _clean_number(value, steps, integer=False)
        case _:
            pass

    if field.pattern and isinstance(value, str):
        value = _fit_pattern(value, field, steps)

    return value, steps


def _clean_email(value: str, steps: list[Transformation]) -> str:
    local, sep, domain = value.partition("@")
    if not sep:
        return value
    lowered = domain.lower()
    if lowered != domain:
        steps.append(
            Transformation(
                rule="email_domain_lowercase",
                before=value,
                after=f"{local}@{lowered}",
                reason="domain names are case-insensitive; the local part is not and is preserved",
            )
        )
    return f"{local}@{lowered}"


def _clean_enum(value: str, field: FieldSpec, steps: list[Transformation]) -> str:
    key = re.sub(r"[^a-z0-9]+", "", value.casefold())
    for allowed in field.allowed or []:
        if re.sub(r"[^a-z0-9]+", "", allowed.casefold()) == key:
            if allowed != value:
                steps.append(
                    Transformation(
                        rule="canonical_enum",
                        before=value,
                        after=allowed,
                        reason=f"matches the allowed value {allowed!r}",
                    )
                )
            return allowed
    raise UnsafeValue(
        f"{value!r} is not one of the allowed values",
        alternatives=list(field.allowed or []),
    )


def _clean_number(value: str, steps: list[Transformation], *, integer: bool) -> object:
    stripped = value.replace(",", "").replace(" ", "")
    try:
        parsed: object = int(stripped) if integer else float(stripped)
    except ValueError as exc:
        raise UnsafeValue(f"{value!r} is not a number") from exc
    if stripped != value:
        steps.append(
            Transformation(
                rule="strip_number_separators",
                before=value,
                after=str(parsed),
                reason="removed thousands separators",
            )
        )
    return parsed


def _clean_date(value: str, field: FieldSpec, plan: ColumnPlan, steps: list[Transformation]) -> str:
    target = field.strftime or "%Y-%m-%d"

    if plan.date_ambiguous_between:
        readings = []
        for fmt in plan.date_ambiguous_between:
            try:
                readings.append(dt.datetime.strptime(value, fmt).strftime(target))
            except ValueError:
                continue
        if len(set(readings)) > 1:
            raise UnsafeValue(
                f"{value!r} could be {' or '.join(sorted(set(readings)))} — nothing in "
                "this column rules either out",
                alternatives=sorted(set(readings)),
            )

    for fmt in ([plan.date_format] if plan.date_format else []) + [target]:
        if not fmt:
            continue
        try:
            parsed = dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
        out = parsed.strftime(target)
        if out != value:
            steps.append(
                Transformation(
                    rule="normalise_date",
                    before=value,
                    after=out,
                    reason=f"read as {fmt} ({'; '.join(plan.notes) or 'unambiguous'})",
                )
            )
        return out

    raise UnsafeValue(f"{value!r} is not a date this column's format explains")


def _fit_pattern(value: str, field: FieldSpec, steps: list[Transformation]) -> str:
    if re.match(field.pattern or "", value):
        return value
    candidate = _PATTERN_NOISE.sub("", value)
    if re.match(field.pattern or "", candidate):
        steps.append(
            Transformation(
                rule="strip_pattern_punctuation",
                before=value,
                after=candidate,
                reason="removed spacing and punctuation to satisfy the field's pattern",
            )
        )
        return candidate
    raise UnsafeValue(
        f"{value!r} does not match the required format and cannot be corrected without "
        "inventing data"
    )


def _require_pattern(value: str, field: FieldSpec) -> None:
    if field.pattern and not re.match(field.pattern, value):
        raise UnsafeValue(
            f"{value!r} does not match the required format, and this field is "
            "case-sensitive so it must not be reshaped"
        )
