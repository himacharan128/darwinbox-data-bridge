"""Compose a mapping score from measured evidence.

This is the mechanism behind "a model's self-reported confidence is not a sufficient
basis for the boundary". Five of the six signals are computed in code from the actual
data; the sixth is the model's opinion, weighted at 0.20 and hard-capped at 0.25 of
the total. No model vote can carry a mapping over the auto-apply line by itself.

Everything here reads the *constraints* a field declares, never a field name, which
is why it generalises to any schema the user brings.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from dbx_contracts import (
    Candidate,
    ColumnMapping,
    ColumnProfile,
    Decision,
    Evidence,
    FieldSpec,
    FieldType,
    MigrationSchema,
    Signal,
    Thresholds,
    Veto,
)

from .profiling import normalize_tokens

#: Below this, the column cannot hold the field's type at all.
TYPE_FIT_FLOOR = 0.50
#: A unique field cannot be fed by a column with this much repetition.
UNIQUE_RATIO_FLOOR = 0.90

_TYPE_PROBE = {
    FieldType.INTEGER: "integer",
    FieldType.NUMBER: "number",
    FieldType.BOOLEAN: "boolean",
    FieldType.EMAIL: "email",
    FieldType.DATE: "date",
    FieldType.DATETIME: "date",
}


def name_similarity(header: str, field: FieldSpec) -> float:
    """Best match of the header against the field name and any declared aliases.

    Blends token overlap with character similarity so 'Date of Joining' scores well
    against date_of_joining, and 'emp_cd' still scores something against employee_id.
    """
    header_tokens = set(normalize_tokens(header))
    best = 0.0
    for candidate in field.match_names():
        cand_tokens = set(normalize_tokens(candidate))
        if not header_tokens or not cand_tokens:
            continue
        overlap = len(header_tokens & cand_tokens)
        jaccard = overlap / len(header_tokens | cand_tokens)
        # Containment matters: "Email" inside "work_email" is strong evidence that
        # Jaccard punishes purely for the field name being longer.
        containment = overlap / min(len(header_tokens), len(cand_tokens))
        ratio = SequenceMatcher(
            None, "".join(sorted(header_tokens)), "".join(sorted(cand_tokens))
        ).ratio()
        best = max(best, 0.65 * max(jaccard, 0.9 * containment) + 0.35 * ratio)
    return round(best, 4)


def type_fit(profile: ColumnProfile, field: FieldSpec) -> float:
    if field.type is FieldType.ENUM:
        # Must use the same normalisation on both sides: casefold() alone leaves the
        # underscore in FULL_TIME, which never matches "Full Time".
        allowed = {_enum_key(a) for a in (field.allowed or [])}
        if not profile.non_null:
            return 0.0
        hits = sum(vc.count for vc in profile.top_values if _enum_key(vc.value) in allowed)
        return round(min(1.0, hits / profile.non_null), 4)
    probe = _TYPE_PROBE.get(field.type)
    if probe is None:
        return 1.0  # plain string accepts anything
    return round(profile.parse_rate(probe), 4)


def _enum_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def relaxed_match(pattern: str, value: str) -> bool:
    """Does the value satisfy the pattern, allowing for safe cleanup?

    Cleanup runs after mapping, so scoring the raw text would make every column that
    merely needs whitespace stripped look like a bad match. Only reversible,
    whitelisted normalisations are tried — never anything that invents data, so a
    bare 10-digit number still fails a +91 pattern.
    """
    if re.match(pattern, value):
        return True
    return bool(re.match(pattern, re.sub(r"[\s\-().]", "", value)))


def constraint_fit(profile: ColumnProfile, field: FieldSpec) -> float:
    """Share of observed values satisfying the field's DISCRIMINATING constraints.

    Only `pattern` and `allowed` count. A max_length of 50 is satisfied by almost
    any text, so rewarding it would be noise; an actual violation is a veto instead.
    """
    values = [vc.value for vc in profile.top_values] or profile.sample
    if not values or not (field.pattern or field.allowed):
        return 0.0
    passes = 0
    for v in values:
        if (
            field.pattern
            and relaxed_match(field.pattern, v)
            or field.allowed
            and _enum_key(v) in {_enum_key(a) for a in field.allowed}
        ):
            passes += 1
    return round(passes / len(values), 4)


def unique_fit(profile: ColumnProfile, field: FieldSpec) -> float:
    return round(min(1.0, profile.cardinality_ratio), 4)


def mask_fit(profile: ColumnProfile, field: FieldSpec) -> float:
    """Does the column's dominant shape match what the field's pattern implies?"""
    dominant = profile.dominant_mask()
    if dominant is None or not profile.sample:
        return 0.0
    sample_ok = sum(1 for v in profile.sample if relaxed_match(field.pattern or "", v))
    return round((sample_ok / len(profile.sample)) * dominant.coverage, 4)


def vetoes_for(profile: ColumnProfile, field: FieldSpec) -> list[Veto]:
    out: list[Veto] = []
    if field.type is not FieldType.STRING and type_fit(profile, field) < TYPE_FIT_FLOOR:
        out.append(Veto.TYPE_INCOMPATIBLE)
    if field.unique and profile.cardinality_ratio < UNIQUE_RATIO_FLOOR:
        out.append(Veto.UNIQUENESS_IMPOSSIBLE)
    if field.max_length is not None and (profile.length_max or 0) > field.max_length:
        out.append(Veto.TYPE_INCOMPATIBLE)
    return out


def score_pair(
    profile: ColumnProfile, field: FieldSpec, *, llm_vote: float | None = None
) -> Evidence:
    """All six signals for one (column, field) pair."""
    signals: dict[Signal, float] = {Signal.NAME_SIM: name_similarity(profile.raw_name, field)}

    # A signal is included only when it can discriminate between fields.
    if field.type is not FieldType.STRING:
        signals[Signal.TYPE_FIT] = type_fit(profile, field)
    if field.pattern or field.allowed:
        signals[Signal.CONSTRAINT_FIT] = constraint_fit(profile, field)
    if field.unique:
        signals[Signal.UNIQUE_FIT] = unique_fit(profile, field)
    if field.pattern:
        signals[Signal.MASK_FIT] = mask_fit(profile, field)
    if llm_vote is not None:
        signals[Signal.LLM_VOTE] = max(0.0, min(1.0, llm_vote))

    evidence = Evidence(signals=signals, vetoes=vetoes_for(profile, field))
    if Veto.TYPE_INCOMPATIBLE in evidence.vetoes:
        if Signal.TYPE_FIT in signals:
            evidence.notes.append(
                f"only {signals[Signal.TYPE_FIT]:.0%} of values parse as {field.type.value}"
            )
        else:
            evidence.notes.append(
                f"values are longer than {field.name}'s limit of {field.max_length}"
            )
    if Veto.UNIQUENESS_IMPOSSIBLE in evidence.vetoes:
        evidence.notes.append(
            f"{field.name} must be unique but the column repeats "
            f"({profile.cardinality_ratio:.0%} distinct)"
        )
    return evidence


def rank_column(
    profile: ColumnProfile,
    schema: MigrationSchema,
    *,
    llm_votes: dict[str, float] | None = None,
    thresholds: Thresholds | None = None,
) -> ColumnMapping:
    """Score one source column against every target field and decide."""
    votes = llm_votes or {}
    thresholds = thresholds or Thresholds()

    candidates = [
        Candidate(
            target_field=f.name,
            evidence=score_pair(profile, f, llm_vote=votes.get(f.name)),
        )
        for f in schema.fields
    ]
    mapping = ColumnMapping(
        source_file=profile.source.file,
        source_sheet=profile.source.sheet,
        source_column=profile.raw_name,
        candidates=[c for c in candidates if c.score > 0],
        thresholds=thresholds,
    )
    return decide(mapping)


def decide(mapping: ColumnMapping) -> ColumnMapping:
    """Apply the boundary: auto-apply, review, or unmapped.

    Auto-apply needs a high score *and* clear separation from the runner-up. A column
    that fits two fields almost equally well is exactly the case a human should see.
    """
    t = mapping.thresholds
    best = mapping.best
    if best is None or best.score < t.review_floor:
        mapping.decision = Decision.UNMAPPED
        mapping.chosen_field = None
    elif best.score >= t.auto_apply and mapping.gap >= t.gap:
        mapping.decision = Decision.AUTO_APPLY
        mapping.chosen_field = best.target_field
    else:
        mapping.decision = Decision.REVIEW
        mapping.chosen_field = None
    return mapping


def assert_llm_within_cap(evidence: Evidence) -> None:
    """Guard: the model's contribution must never exceed the declared cap."""
    from dbx_contracts import LLM_VOTE_CAP

    if evidence.llm_share > LLM_VOTE_CAP + 1e-9:
        raise ValueError(
            f"model vote contributed {evidence.llm_share:.2%} of the score, "
            f"above the {LLM_VOTE_CAP:.0%} cap"
        )
