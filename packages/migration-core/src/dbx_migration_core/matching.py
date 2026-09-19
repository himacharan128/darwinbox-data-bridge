"""Decide which records describe the same entity.

Fully deterministic — the model has no say here. That is what lets "no known
incorrect automatic merges" be a property of code you can prove rather than a
behaviour you observe and hope holds.

Candidate pairs come from declared blocking keys. Comparing every record with every
other is O(n^2): at the stated ceiling of 50,000 records per run that is 1.25 billion
comparisons, which does not finish. Blocking makes the work proportional to the number
of records that plausibly collide.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import StrEnum
from itertools import combinations

from dbx_contracts import MigrationSchema

#: Field weights when comparing a pair. A unique field is much stronger evidence.
W_UNIQUE = 3.0
W_REQUIRED = 2.0
W_OPTIONAL = 1.0


class MatchDecision(StrEnum):
    AUTO_MERGE = "auto_merge"
    REVIEW = "review"
    NO_MATCH = "no_match"


@dataclass
class PairEvidence:
    left: str
    right: str
    agreed: list[str] = dc_field(default_factory=list)
    conflicted: list[str] = dc_field(default_factory=list)
    unique_agreed: list[str] = dc_field(default_factory=list)
    unique_conflicted: list[str] = dc_field(default_factory=list)
    score: float = 0.0
    decision: MatchDecision = MatchDecision.NO_MATCH
    reason: str = ""
    blocked_by: list[str] = dc_field(default_factory=list)


def _key(values: dict[str, object], fields: list[str], schema: MigrationSchema) -> str | None:
    parts: list[str] = []
    for name in fields:
        value = values.get(name)
        if value is None or value == "":
            return None
        spec = schema.by_name.get(name)
        text = str(value)
        parts.append(text if (spec and spec.case_sensitive) else text.casefold())
    return "|".join(parts)


def candidate_pairs(
    records: list[tuple[str, dict[str, object]]], schema: MigrationSchema
) -> dict[tuple[str, str], list[str]]:
    """Pairs worth comparing, with the blocking key that surfaced each one."""
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    for block in schema.blocking_keys:
        buckets: dict[str, list[str]] = defaultdict(list)
        for record_id, values in records:
            key = _key(values, block, schema)
            if key is not None:
                buckets[key].append(record_id)
        label = "+".join(block)
        for ids in buckets.values():
            if len(ids) < 2:
                continue
            for left, right in combinations(sorted(ids), 2):
                found[(left, right)].append(label)
    return found


def compare(
    left_id: str,
    left: dict[str, object],
    right_id: str,
    right: dict[str, object],
    schema: MigrationSchema,
    *,
    blocked_by: list[str] | None = None,
) -> PairEvidence:
    ev = PairEvidence(left=left_id, right=right_id, blocked_by=blocked_by or [])
    weight_total = weight_agreed = 0.0

    for spec in schema.fields:
        a, b = left.get(spec.name), right.get(spec.name)
        if a in (None, "") or b in (None, ""):
            continue
        weight = W_UNIQUE if spec.unique else (W_REQUIRED if spec.required else W_OPTIONAL)
        same = str(a) == str(b) if spec.case_sensitive else str(a).casefold() == str(b).casefold()
        weight_total += weight
        if same:
            weight_agreed += weight
            ev.agreed.append(spec.name)
            if spec.unique:
                ev.unique_agreed.append(spec.name)
        else:
            ev.conflicted.append(spec.name)
            if spec.unique:
                ev.unique_conflicted.append(spec.name)

    ev.score = round(weight_agreed / weight_total, 4) if weight_total else 0.0
    return _decide(ev, schema)


def _decide(ev: PairEvidence, schema: MigrationSchema) -> PairEvidence:
    spec = schema.matching

    if ev.unique_conflicted:
        # Two records that disagree on an identifier are not the same record, whatever
        # else they share. A namesake with the same birthday is a question, not a merge.
        ev.decision = (
            MatchDecision.REVIEW if ev.score >= spec.review_floor else MatchDecision.NO_MATCH
        )
        ev.reason = (
            f"{', '.join(ev.unique_conflicted)} differ, so these cannot be merged "
            f"automatically; {len(ev.agreed)} other fields agree"
        )
        return ev

    if ev.unique_agreed:
        ev.decision = MatchDecision.AUTO_MERGE
        ev.reason = f"identical {', '.join(ev.unique_agreed)}"
        return ev

    for forbidden in spec.never_merge_on:
        if set(ev.agreed) and set(ev.agreed) <= set(forbidden):
            ev.decision = (
                MatchDecision.REVIEW if ev.score >= spec.review_floor else MatchDecision.NO_MATCH
            )
            ev.reason = (
                f"the only agreement is on {', '.join(sorted(ev.agreed))}, which never "
                "justifies an automatic merge"
            )
            return ev

    if ev.score >= spec.auto_merge:
        ev.decision = MatchDecision.AUTO_MERGE
        ev.reason = f"{len(ev.agreed)} fields agree and none conflict"
    elif ev.score >= spec.review_floor:
        ev.decision = MatchDecision.REVIEW
        ev.reason = f"{len(ev.agreed)} fields agree, {len(ev.conflicted)} conflict"
    else:
        ev.decision = MatchDecision.NO_MATCH
        ev.reason = "too little in common"
    return ev


def match_records(
    records: list[tuple[str, dict[str, object]]], schema: MigrationSchema
) -> list[PairEvidence]:
    by_id = dict(records)
    out: list[PairEvidence] = []
    for (left, right), blocks in candidate_pairs(records, schema).items():
        out.append(compare(left, by_id[left], right, by_id[right], schema, blocked_by=blocks))
    return sorted(out, key=lambda e: -e.score)


def conflicting_fields(
    left: dict[str, object], right: dict[str, object], schema: MigrationSchema
) -> list[tuple[str, object, object]]:
    """Fields that disagree between two records already known to be the same entity.

    Identity and field conflicts are separate decisions: establishing that two rows
    are the same person says nothing about which job title is correct.
    """
    out = []
    for spec in schema.fields:
        a, b = left.get(spec.name), right.get(spec.name)
        if a in (None, "") or b in (None, ""):
            continue
        same = str(a) == str(b) if spec.case_sensitive else str(a).casefold() == str(b).casefold()
        if not same:
            out.append((spec.name, a, b))
    return out
