"""One run, end to end: extract, understand, map, reconcile, clean, validate, escalate.

The shape of the autonomy boundary lives here. Everything that can be decided from
measured evidence is decided; everything that cannot becomes a review case carrying
the evidence and the question, so a consultant can answer it without opening a file.

Delivery is deliberately not part of this. A record becoming ready and a record being
accepted by a destination are different facts and are recorded separately.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Protocol

from dbx_contracts import (
    Action,
    Actor,
    AuditEvent,
    CanonicalRecord,
    ColumnMapping,
    ColumnProfile,
    Decision,
    EscalationClass,
    ExtractedRecord,
    FieldProvenance,
    FieldSpec,
    MigrationSchema,
    Option,
    RecordState,
    ReviewCase,
    SourceRef,
    Transformation,
)

from .cleanup import ColumnPlan, UnsafeValue, clean, plan_column
from .matching import MatchDecision, conflicting_fields, match_records
from .profiling import profile_column
from .scoring import rank_column
from .validation import check_uniqueness, validate_record

#: The bounded correction cycle: validate, apply one safe correction, validate again.
MAX_VALIDATION_ATTEMPTS = 2


class VoteSource(Protocol):
    def __call__(self, profile: ColumnProfile, schema: MigrationSchema) -> dict[str, float]: ...


@dataclass
class Overrides:
    """Human decisions, content-addressed so they survive a re-run.

    Resolving a case re-runs the pipeline with the decision applied rather than
    patching state in place. Re-running is deterministic and cheap, and it means a
    decision cannot leave the dataset half-corrected — the reprocess-and-revalidate
    step the brief asks for comes free instead of being a separate code path.

    Keys are content, not case ids, because case ids are positional and a re-run
    renumbers them.
    """

    column_map: dict[tuple[str, str], str] = dc_field(default_factory=dict)
    constants: dict[tuple[str | None, str], object] = dc_field(default_factory=dict)
    values: dict[tuple[str, str], str] = dc_field(default_factory=dict)
    merge: dict[tuple[str, str], bool] = dc_field(default_factory=dict)
    excluded: set[str] = dc_field(default_factory=set)

    def is_empty(self) -> bool:
        return not (
            self.column_map or self.constants or self.values or self.merge or self.excluded
        )


@dataclass
class RunResult:
    records: list[CanonicalRecord] = dc_field(default_factory=list)
    mappings: list[ColumnMapping] = dc_field(default_factory=list)
    cases: list[ReviewCase] = dc_field(default_factory=list)
    audit: list[AuditEvent] = dc_field(default_factory=list)
    lookups: dict[str, set[str]] = dc_field(default_factory=dict)

    @property
    def ready(self) -> list[CanonicalRecord]:
        return [r for r in self.records if r.state is RecordState.READY]

    @property
    def blocked(self) -> list[CanonicalRecord]:
        return [r for r in self.records if r.state is RecordState.BLOCKED]

    @property
    def open_cases(self) -> list[ReviewCase]:
        return [c for c in self.cases if c.state.value == "open"]


class Pipeline:
    def __init__(
        self,
        run_id: str,
        schema: MigrationSchema,
        votes: VoteSource | None = None,
        overrides: Overrides | None = None,
    ):
        self.run_id = run_id
        self.schema = schema
        self.votes = votes
        self.overrides = overrides or Overrides()
        self.result = RunResult()

    # ---------------------------------------------------------------- audit

    def _log(self, action: str, summary: str, *, actor: Actor = Actor.AGENT, **kw) -> None:
        self.result.audit.append(
            AuditEvent(
                id=str(uuid.uuid4()), run_id=self.run_id, actor=actor,
                action=action, summary=summary, **kw,
            )
        )

    def _case(self, klass: EscalationClass, headline: str, detail: str, **kw) -> ReviewCase:
        case = ReviewCase(
            id=f"case-{len(self.result.cases) + 1:03d}", run_id=self.run_id,
            klass=klass, headline=headline, detail=detail, **kw,
        )
        self.result.cases.append(case)
        self._log(
            "escalation.raised", headline, case_id=case.id,
            target_field=case.target_field, reason=detail,
            detail={"class": klass.value, "evidence": case.evidence},
        )
        return case

    # ---------------------------------------------------------------- stages

    def run(
        self, sources: dict[str, list[ExtractedRecord]], lookups: dict[str, set[str]]
    ) -> RunResult:
        self.result.lookups = lookups
        self._log("run.started", f"Reading {len(sources)} file(s)")
        self.value_case: dict[tuple[str, str], str] = {}

        mappings = self._map(sources)
        records = self._build(sources, mappings)
        records = self._reconcile(records)
        self._validate(records)
        self._finalise()
        return self.result

    def _map(self, sources: dict[str, list[ExtractedRecord]]) -> dict[str, dict[str, str]]:
        """Score every source column against the schema and apply what is clear."""
        applied: dict[str, dict[str, str]] = defaultdict(dict)
        self.plans: dict[tuple[str, str], ColumnPlan] = {}
        self.profiles: dict[tuple[str, str], ColumnProfile] = {}
        # (file, target_field) -> case id, so a record missing a field because its
        # column mapping is unresolved attaches to that one case instead of raising
        # its own. Without this, one ambiguous column produces a MISSING_REQUIRED
        # case per record and the queue becomes unusable.
        self.field_case: dict[tuple[str | None, str], str] = {}

        for name, records in sources.items():
            if not records:
                continue
            columns: dict[str, list[str | None]] = defaultdict(list)
            for record in records:
                for header, value in record.values.items():
                    columns[header].append(value)
            src = records[0].source

            for header, values in columns.items():
                profile = profile_column(
                    SourceRef(file=src.file, sheet=src.sheet, column=header), header, values
                )
                self.profiles[(name, header)] = profile
                llm = self.votes(profile, self.schema) if self.votes else None
                mapping = rank_column(
                    profile, self.schema, llm_votes=llm, lookups=self.result.lookups
                )
                self.result.mappings.append(mapping)

                forced = self.overrides.column_map.get((name, header))
                if forced:
                    mapping.decision = Decision.AUTO_APPLY
                    mapping.chosen_field = forced
                    self._log(
                        "mapping.human_applied",
                        f"Mapped {header!r} to {forced} as you decided",
                        actor=Actor.HUMAN, target_field=forced, source_refs=[profile.source],
                    )

                if mapping.decision is Decision.AUTO_APPLY and mapping.chosen_field:
                    applied[name][header] = mapping.chosen_field
                    spec = self.schema.by_name[mapping.chosen_field]
                    self.plans[(name, header)] = plan_column(profile, spec)
                    self._log(
                        "mapping.auto_applied",
                        f"Mapped {header!r} to {mapping.chosen_field}",
                        target_field=mapping.chosen_field, source_refs=[profile.source],
                        reason="; ".join(mapping.best.evidence.explain()) if mapping.best else "",
                        detail={"score": mapping.best.score if mapping.best else 0,
                                "gap": mapping.gap},
                    )
                elif mapping.decision is Decision.REVIEW:
                    case = self._ambiguous_mapping(name, header, mapping, profile)
                    for candidate in mapping.ranked[:3]:
                        self.field_case.setdefault((name, candidate.target_field), case.id)

        self._unmapped_required(applied, list(sources))
        return applied

    def _ambiguous_mapping(
        self, file: str, header: str, mapping: ColumnMapping, profile: ColumnProfile
    ) -> ReviewCase:
        ranked = mapping.ranked[:3]
        lines = [
            f"{c.target_field}: {'; '.join(c.evidence.explain())}" for c in ranked
        ]
        return self._case(
            EscalationClass.AMBIGUOUS_MAPPING,
            headline=f"Which field does {header!r} belong to?",
            detail=(
                f"{', '.join(c.target_field for c in ranked[:2])} both fit, and the "
                f"evidence does not separate them (gap {mapping.gap:.2f})."
            ),
            target_field=None,
            source_refs=[profile.source],
            raw_values=profile.sample[:5],
            evidence={"candidates": lines, "profile": profile.for_model()},
            actions=[Action.CORRECT, Action.REJECT],
            options=[
                Option(label=f"Map to {c.target_field}", value=c.target_field,
                       description="; ".join(c.evidence.explain()[:2]))
                for c in ranked
            ],
        )

    def _unmapped_required(
        self, applied: dict[str, dict[str, str]], files: list[str]
    ) -> None:
        """Ask once per gap, not once per record.

        A required field with no mapping is a question about a FILE, not about each of
        its rows. Raising it per record turns one answerable question into hundreds of
        identical ones and buries the cases that genuinely differ.
        """
        everywhere = {field for cols in applied.values() for field in cols.values()}

        for spec in self.schema.required_fields:
            if spec.name in everywhere:
                missing_from = [
                    f for f in files
                    if spec.name not in set(applied.get(f, {}).values())
                    and (f, spec.name) not in self.field_case
                ]
            elif (None, spec.name) in self.field_case:
                continue
            else:
                missing_from = [None]

            for origin in missing_from:
                if origin is not None and (origin, spec.name) in self.field_case:
                    continue
                self._raise_unmapped(spec, origin)

    def _raise_unmapped(self, spec: FieldSpec, origin: str | None) -> None:
        near = sorted(
            (
                m for m in self.result.mappings
                if m.best
                and m.best.target_field == spec.name
                and (origin is None or m.source_file == origin)
            ),
            key=lambda m: -(m.best.score if m.best else 0),
        )[:3]

        where = origin or "any uploaded file"
        if near:
            closest = near[0]
            detail = (
                f"The closest column is {closest.source_column!r}, which scored "
                f"{closest.best.score:.2f} — below the level needed to apply it without "
                "asking. Confirm it, choose another column, or exclude these records."
            )
        else:
            detail = (
                f"No column in {where} resembles {spec.name}. Supply a value for every "
                "record from this file, or exclude them."
            )

        case = self._case(
            EscalationClass.UNMAPPED_REQUIRED,
            headline=f"{where} has no column for {spec.name}",
            detail=detail,
            target_field=spec.name,
            source_refs=[m.best and SourceRef(file=m.source_file, column=m.source_column)
                         for m in near[:1]] if near else [],
            evidence={
                "closest": [
                    f"{m.source_column!r} in {m.source_file} scored {m.best.score:.2f}"
                    f" ({'; '.join(m.best.evidence.explain()[:2])})"
                    for m in near if m.best
                ] or ["no column scored above the floor"],
                "allowed": spec.allowed or [],
            },
            actions=[Action.CORRECT, Action.REJECT],
            options=[
                *[Option(label=f"Use column {m.source_column!r}", value=f"column:{m.source_column}",
                         description=f"scored {m.best.score:.2f}") for m in near if m.best],
                *[Option(label=f"Set every record to {v}", value=f"constant:{v}")
                  for v in (spec.allowed or [])],
            ],
        )
        self.field_case[(origin, spec.name)] = case.id

    def _build(
        self, sources: dict[str, list[ExtractedRecord]], applied: dict[str, dict[str, str]]
    ) -> list[CanonicalRecord]:
        """Apply mappings and safe cleanup, one extracted row at a time."""
        out: list[CanonicalRecord] = []
        for name, records in sources.items():
            columns = applied.get(name, {})
            if not columns:
                continue
            for extracted in records:
                record = CanonicalRecord(
                    id=extracted.id, run_id=self.run_id, contributing=[extracted.source]
                )
                # Identity first: a case raised while building a record must be able
                # to name that record, and "EMP-00016" is resolvable in a queue where a
                # UUID is not.
                identity = self._identity_field()
                ordered = sorted(
                    columns.items(), key=lambda kv: (kv[1] != identity, kv[0])
                )
                for header, field_name in ordered:
                    spec = self.schema.by_name[field_name]
                    raw = extracted.get(header)
                    ref = SourceRef(
                        file=extracted.source.file, sheet=extracted.source.sheet,
                        row=extracted.source.row, column=header,
                    )
                    self._apply_value(record, spec, raw, ref, self.plans.get((name, header)))
                self._apply_constants(record, name)
                out.append(record)
        return out

    def _apply_constants(self, record: CanonicalRecord, origin: str) -> None:
        """Fill fields a human chose to set for every record from a file."""
        for (scope, field_name), value in self.overrides.constants.items():
            if scope not in (None, origin) or field_name in record.values:
                continue
            spec = self.schema.by_name.get(field_name)
            if spec is None:
                continue
            record.values[field_name] = value
            record.provenance[field_name] = FieldProvenance(
                target_field=field_name,
                source=record.contributing[0],
                raw_value=None,
                value=value,
                transformations=[
                    Transformation(
                        rule="human_constant", before=None, after=str(value),
                        reason="set for every record from this file by a reviewer",
                    )
                ],
            )

    def _apply_value(
        self,
        record: CanonicalRecord,
        spec: FieldSpec,
        raw: str | None,
        ref: SourceRef,
        plan: ColumnPlan | None,
    ) -> None:
        key = self._record_key_for(record)
        override = self.overrides.values.get((key, spec.name)) if key else None
        if override is not None:
            record.values[spec.name] = override
            record.provenance[spec.name] = FieldProvenance(
                target_field=spec.name, source=ref, raw_value=raw, value=override,
                transformations=[
                    Transformation(
                        rule="human_correction", before=raw, after=str(override),
                        reason="corrected by a reviewer",
                    )
                ],
            )
            return

        try:
            value, steps = clean(raw, spec, plan)
        except UnsafeValue as unsafe:
            record.state = RecordState.BLOCKED
            case = self._case(
                EscalationClass.AMBIGUOUS_VALUE if unsafe.alternatives
                else EscalationClass.VALIDATION_UNRESOLVED,
                headline=f"{spec.name} could not be cleaned safely",
                detail=unsafe.reason,
                record_key=record.natural_key or record.id,
                target_field=spec.name,
                source_refs=[ref],
                raw_values=[raw or ""],
                rule=spec.pattern or (", ".join(spec.allowed) if spec.allowed else None),
                actions=[Action.CORRECT, Action.REJECT],
                options=[Option(label=alt, value=alt) for alt in unsafe.alternatives],
                blocks_records=[record.id],
            )
            record.open_cases.append(case.id)
            # Remember it so validation does not report the same field as simply missing.
            self.value_case[(record.id, spec.name)] = case.id
            return

        record.values[spec.name] = value
        record.provenance[spec.name] = FieldProvenance(
            target_field=spec.name, source=ref, raw_value=raw, value=value,
            transformations=steps,
        )
        for step in steps:
            self._log(
                "cleanup.applied", f"{spec.name}: {step.reason}",
                record_id=record.id, target_field=spec.name,
                before=step.before, after=step.after, reason=step.reason,
                source_refs=[ref],
            )

    def _reconcile(self, records: list[CanonicalRecord]) -> list[CanonicalRecord]:
        """Merge records that are demonstrably the same entity; ask about the rest."""
        pairs = match_records([(r.id, r.values) for r in records], self.schema)
        by_id = {r.id: r for r in records}
        merged_into: dict[str, str] = {}

        for pair in pairs:
            left = by_id.get(self._root(pair.left, merged_into))
            right = by_id.get(self._root(pair.right, merged_into))
            if left is None or right is None or left.id == right.id:
                continue

            keys = (self._record_key_for(left) or left.id, self._record_key_for(right) or right.id)
            chosen = self.overrides.merge.get(keys)
            if chosen is not None:
                if chosen:
                    self._merge(left, right, "confirmed by a reviewer")
                    merged_into[right.id] = left.id
                    by_id.pop(right.id, None)
                else:
                    self._log(
                        "records.kept_separate",
                        f"Kept {keys[0]} and {keys[1]} as different people, as you decided",
                        actor=Actor.HUMAN,
                    )
                continue

            if pair.decision is MatchDecision.AUTO_MERGE:
                self._merge(left, right, pair.reason)
                merged_into[right.id] = left.id
                by_id.pop(right.id, None)
            elif pair.decision is MatchDecision.REVIEW:
                case = self._case(
                    EscalationClass.UNCERTAIN_IDENTITY,
                    headline="Are these the same person?",
                    detail=pair.reason,
                    record_key=str(left.values.get(self._identity_field(), left.id)),
                    source_refs=[*left.contributing, *right.contributing],
                    evidence={
                        "agree": pair.agreed, "conflict": pair.conflicted,
                        "found_by": pair.blocked_by, "score": pair.score,
                        "keys": [
                            self._record_key_for(left) or left.id,
                            self._record_key_for(right) or right.id,
                        ],
                        "left": {k: str(v) for k, v in left.values.items()},
                        "right": {k: str(v) for k, v in right.values.items()},
                    },
                    actions=[Action.APPROVE, Action.REJECT],
                    options=[
                        Option(label="Same person — merge", value="merge"),
                        Option(label="Different people — keep separate", value="separate",
                               recommended=True),
                    ],
                    blocks_records=[left.id, right.id],
                )
                left.open_cases.append(case.id)
                right.open_cases.append(case.id)
                left.state = right.state = RecordState.BLOCKED

        return [r for r in records if r.id not in merged_into]

    def _record_key_for(self, record: CanonicalRecord) -> str | None:
        value = record.values.get(self._identity_field())
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _root(record_id: str, merged: dict[str, str]) -> str:
        while record_id in merged:
            record_id = merged[record_id]
        return record_id

    def _identity_field(self) -> str:
        keys = self.schema.blocking_keys
        return keys[0][0] if keys and keys[0] else self.schema.fields[0].name

    def _merge(self, keep: CanonicalRecord, other: CanonicalRecord, reason: str) -> None:
        conflicts = conflicting_fields(keep.values, other.values, self.schema)
        for name, value in other.values.items():
            if keep.values.get(name) in (None, ""):
                keep.values[name] = value
                if name in other.provenance:
                    keep.provenance[name] = other.provenance[name]
        keep.contributing.extend(other.contributing)
        self._log(
            "records.merged", f"Combined two rows describing the same entity ({reason})",
            record_id=keep.id, source_refs=other.contributing, reason=reason,
        )

        for name, mine, theirs in conflicts:
            case = self._case(
                EscalationClass.CONFLICTING_FACTS,
                headline=f"Two sources disagree about {name}",
                detail=(
                    f"Identity is certain — {reason}. But the sources give different "
                    f"values for {name} and no authority rule settles it."
                ),
                record_key=str(keep.values.get(self._identity_field(), keep.id)),
                target_field=name,
                source_refs=[*keep.contributing, *other.contributing],
                raw_values=[str(mine), str(theirs)],
                evidence={"values": {str(mine): keep.contributing[0].label(),
                                     str(theirs): other.contributing[0].label()}},
                actions=[Action.CORRECT, Action.REJECT],
                options=[Option(label=str(mine), value=str(mine)),
                         Option(label=str(theirs), value=str(theirs))],
                blocks_records=[keep.id],
            )
            keep.open_cases.append(case.id)
            keep.state = RecordState.BLOCKED

    def _validate(self, records: list[CanonicalRecord]) -> None:
        self.result.records = records
        identity = self._identity_field()
        known = {self.schema.entity: {
            str(r.values.get(identity)) for r in records if r.values.get(identity)
        }}
        dupes = check_uniqueness([(r.id, r.values) for r in records], self.schema)

        for record in records:
            record.validation_attempts = 1
            result = validate_record(
                record.values, self.schema, lookups=self.result.lookups, known_keys=known
            )
            result.issues.extend(dupes.get(record.id, []))
            record.validation = result
            if result.ok:
                continue

            # One bounded correction cycle, then stop. Repeating an identical failed
            # check is not a second attempt, it is a loop.
            record.validation_attempts = MAX_VALIDATION_ATTEMPTS
            origin = record.contributing[0].file if record.contributing else None
            for issue in result.errors:
                if issue.rule == "required" and issue.target_field:
                    if (record.id, issue.target_field) in self.value_case:
                        continue
                    existing = self.field_case.get((origin, issue.target_field)) or \
                        self.field_case.get((None, issue.target_field))
                    if existing:
                        self._attach(record, existing)
                        continue
                klass = {
                    "required": EscalationClass.MISSING_REQUIRED,
                    "reference:missing": EscalationClass.UNRESOLVED_REFERENCE,
                    "reference:self": EscalationClass.UNRESOLVED_REFERENCE,
                }.get(issue.rule, EscalationClass.VALIDATION_UNRESOLVED)

                options: list[Option] = []
                if klass is EscalationClass.UNRESOLVED_REFERENCE and issue.target_field:
                    spec = self.schema.by_name[issue.target_field]
                    ref = spec.reference_target
                    if ref and ref[0] in self.result.lookups:
                        options = [Option(label=v, value=v)
                                   for v in sorted(self.result.lookups[ref[0]])[:8]]

                case = self._case(
                    klass,
                    headline=issue.message,
                    detail=(
                        f"Checked against the approved schema after "
                        f"{record.validation_attempts} attempts; no safe correction "
                        "is available."
                    ),
                    record_key=str(record.values.get(identity, record.id)),
                    target_field=issue.target_field,
                    source_refs=record.contributing,
                    raw_values=[issue.value] if issue.value else [],
                    rule=issue.rule,
                    attempts=[f"attempt {record.validation_attempts}: {issue.rule} failed"],
                    actions=[Action.CORRECT, Action.REJECT],
                    options=options,
                    blocks_records=[record.id],
                )
                record.open_cases.append(case.id)
            record.state = RecordState.BLOCKED

    def _attach(self, record: CanonicalRecord, case_id: str) -> None:
        """Block a record on an existing case rather than raising a duplicate."""
        case = next((c for c in self.result.cases if c.id == case_id), None)
        if case is None:
            return
        if record.id not in case.blocks_records:
            case.blocks_records.append(record.id)
        if case_id not in record.open_cases:
            record.open_cases.append(case_id)

    def _finalise(self) -> None:
        for record in self.result.records:
            key = self._record_key_for(record)
            if key and key in self.overrides.excluded:
                record.state = RecordState.EXCLUDED
                record.open_cases.clear()
                continue
            if record.open_cases or not record.validation.ok:
                record.state = RecordState.BLOCKED
            else:
                record.state = RecordState.READY
        ready = len(self.result.ready)
        self._log(
            "run.processed",
            f"{ready} record(s) ready, {len(self.result.blocked)} awaiting a decision, "
            f"{len(self.result.open_cases)} question(s) raised",
        )


def apply_decision(
    overrides: Overrides, case: ReviewCase, action: Action, value: str | None
) -> Overrides:
    """Translate one human decision into a re-runnable override.

    Rejection never deletes source data. It marks the affected records excluded so
    they are not delivered, and the decision stays in the audit trail.
    """
    if action is Action.REJECT:
        if case.record_key:
            overrides.excluded.add(case.record_key)
        for record_id in case.blocks_records:
            overrides.excluded.add(record_id)
        return overrides

    match case.klass:
        case EscalationClass.AMBIGUOUS_MAPPING if value:
            ref = case.source_refs[0] if case.source_refs else None
            if ref and ref.column:
                overrides.column_map[(ref.file, ref.column)] = value

        case EscalationClass.UNMAPPED_REQUIRED if value:
            scope = case.source_refs[0].file if case.source_refs else None
            if value.startswith("column:"):
                column = value.split(":", 1)[1]
                if scope:
                    overrides.column_map[(scope, column)] = case.target_field or ""
            else:
                constant = value.split(":", 1)[1] if value.startswith("constant:") else value
                key_scope = scope if "has no column" in case.headline and scope else None
                overrides.constants[(key_scope, case.target_field or "")] = constant

        case EscalationClass.UNCERTAIN_IDENTITY:
            keys = tuple(case.evidence.get("keys", ())) if case.evidence else ()
            if len(keys) == 2:
                overrides.merge[(str(keys[0]), str(keys[1]))] = (
                    action is Action.APPROVE and value != "separate"
                )

        case _ if value and case.record_key and case.target_field:
            overrides.values[(case.record_key, case.target_field)] = value

    return overrides
