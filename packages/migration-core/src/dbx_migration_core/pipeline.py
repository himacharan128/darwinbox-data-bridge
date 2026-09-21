"""One run, end to end: extract, understand, map, reconcile, clean, validate, escalate.

The shape of the autonomy boundary lives here. Everything that can be decided from
measured evidence is decided; everything that cannot becomes a review case carrying
the evidence and the question, so a consultant can answer it without opening a file.

Delivery is deliberately not part of this. A record becoming ready and a record being
accepted by a destination are different facts and are recorded separately.
"""
from __future__ import annotations

import hashlib
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
    ValidationResult,
)

from .cleanup import ColumnPlan, UnsafeValue, clean, plan_column
from .matching import MatchDecision, conflicting_fields, match_records
from .profiling import profile_column
from .scoring import decide, rank_column
from .validation import check_uniqueness, validate_record

#: The bounded correction cycle: validate, apply one safe correction, validate again.
MAX_VALIDATION_ATTEMPTS = 2

#: A value read below this is worth doubting when it also fails its field's rules.
LOW_READ_CONFIDENCE = 0.80

#: Below this share of required fields, a file is not the entity at all.
UNRECOGNISED_FILE_RATIO = 0.30


class VoteSource(Protocol):
    """One call per file, not per column, so columns are judged among their siblings."""

    def __call__(
        self, file_name: str, profiles: list[ColumnProfile], schema: MigrationSchema
    ) -> dict[str, dict[str, float]]: ...


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
    #: How to read a column whose values are shaped so that nothing in the data
    #: settles it - 01/04/2015 in a column where no day exceeds 12. That is one fact
    #: about the column, so it is one question, and the answer applies to every row.
    formats: dict[tuple[str, str], str] = dc_field(default_factory=dict)
    #: What a value that fits no allowed option should become, per (file, column,
    #: value). 'INTERN' in a column of four interns is one question, not four.
    value_map: dict[tuple[str, str, str], str] = dc_field(default_factory=dict)
    #: Files a reviewer said are not this entity at all. Their rows are not read.
    ignored_files: set[str] = dc_field(default_factory=set)
    #: Files this client trusts, most authoritative first. When two exports disagree
    #: about the same person, the answer is almost never "it depends which record" -
    #: it is "the HR system is right and payroll is stale". Said once, it settles
    #: every disagreement of that shape in the run and every run after it.
    authority: list[str] = dc_field(default_factory=list)

    def prefer(self, left: str, right: str) -> str | None:
        """Which of two files wins, or None if the client has not said.

        A named file beats an unnamed one. "Believe the HR export" is a statement
        about the HR export, not about the one other file it happened to disagree
        with first, so ranking one file settles every disagreement it is part of.
        """
        rank = {name: i for i, name in enumerate(self.authority)}
        a, b = rank.get(left), rank.get(right)
        if a == b:
            return None
        if a is None:
            return right
        if b is None:
            return left
        return left if a < b else right

    def is_empty(self) -> bool:
        return not (
            self.column_map or self.constants or self.values or self.merge
            or self.excluded or self.authority or self.formats
            or self.value_map or self.ignored_files
        )


@dataclass
class RunResult:
    #: Raw rows read from the files, before anything is reconciled. Showing this beside
    #: the employee count is what makes reconciliation visible.
    rows_read: int = 0
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


def _resolve_within_file(
    ranked: list[tuple[str, ColumnMapping]],
) -> list[tuple[str, ColumnMapping]]:
    """Stop two columns of one file being applied to the same field.

    Scoring each column alone lets 'Emp ID' and 'Manager ID' both land on
    employee_id. Across files that is correct and necessary - complementary
    exports describe the same people - but inside one file it means the weaker
    claim belongs somewhere else, so it is taken back and asked again.

    Only ever taken back. Eliminating a rival proves this column is not that
    field; it does not prove it is the next one down, because plenty of columns
    belong to no field at all. Promoting on elimination read 'the only date field
    left is date_of_joining' and mapped a probation-end date onto it.
    """
    winner: dict[str, tuple[str, ColumnMapping]] = {}
    for header, mapping in ranked:
        field = mapping.chosen_field
        if mapping.decision is not Decision.AUTO_APPLY or not field or not mapping.best:
            continue
        held = winner.get(field)
        if held is None or mapping.best.score > held[1].best.score:
            winner[field] = (header, mapping)

    for field, (owner, _) in winner.items():
        for header, mapping in ranked:
            if header == owner or mapping.chosen_field != field:
                continue
            mapping.candidates = [
                c for c in mapping.candidates if c.target_field != field
            ]
            mapping.chosen_field = None
            decide(mapping)
    return ranked


class Pipeline:
    def __init__(
        self,
        run_id: str,
        schema: MigrationSchema,
        votes: VoteSource | None = None,
        overrides: Overrides | None = None,
        confidence: dict[str, dict[str, float]] | None = None,
    ):
        self.run_id = run_id
        self.schema = schema
        self.votes = votes
        self.overrides = overrides or Overrides()
        # Per-cell read confidence, for values that came off a scan rather than a file.
        self.confidence = confidence or {}
        self.result = RunResult()
        self.merged_into: dict[str, str] = {}
        self.format_rows: dict[str, list[tuple[str, str]]] = defaultdict(list)

    # ---------------------------------------------------------------- audit

    def _log(self, action: str, summary: str, *, actor: Actor = Actor.AGENT, **kw) -> None:
        # Identity comes from content, not from a fresh uuid. A run is replayed on every
        # read, and random ids would append the same history again each time.
        seq = len(self.result.audit)
        digest = hashlib.sha256(
            f"{self.run_id}|{seq}|{action}|{summary}|{kw.get('record_id')}".encode()
        ).hexdigest()[:32]
        self.result.audit.append(
            AuditEvent(
                id=digest, run_id=self.run_id, actor=actor,
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

    def _attach_to_case(self, case_id: str, record: CanonicalRecord, field: str) -> None:
        """Ride on a question already asked rather than asking it again."""
        case = next((c for c in self.result.cases if c.id == case_id), None)
        if case is None:
            return
        if record.id not in case.blocks_records:
            case.blocks_records.append(record.id)
        record.state = RecordState.BLOCKED
        if case_id not in record.open_cases:
            record.open_cases.append(case_id)
        self.value_case[(record.id, field)] = case_id

    # ---------------------------------------------------------------- stages

    def run(
        self, sources: dict[str, list[ExtractedRecord]], lookups: dict[str, set[str]]
    ) -> RunResult:
        self.result.lookups = lookups
        for name in sorted(self.overrides.ignored_files & set(sources)):
            self._log("file.ignored", f"Ignored {name}, as you decided", actor=Actor.HUMAN)
        sources = {k: v for k, v in sources.items() if k not in self.overrides.ignored_files}
        self.result.rows_read = sum(len(records) for records in sources.values())
        self._log(
            "run.started",
            f"Reading {len(sources)} file(s), {self.result.rows_read} rows",
        )
        self.value_case: dict[tuple[str, str], str] = {}
        #: One question per value that fits no allowed option, per column.
        self.value_group: dict[tuple[str, str, str], str] = {}

        mappings = self._map(sources)
        records = self._build(sources, mappings)
        records = self._reconcile(records)
        self._cross_check_formats(records)
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
        #: One question per column for facts that are true of the column rather than
        #: of a row. A date column nothing can disambiguate is one question about the
        #: column, not one per employee who happens to have joined on the 4th.
        self.format_case: dict[tuple[str, str], str] = {}
        #: Every row waiting on a column's reading, with the value it holds, so the
        #: column can be checked against what other files say about the same people.
        self.format_rows: dict[str, list[tuple[str, str]]] = defaultdict(list)

        for name, records in sources.items():
            if not records:
                continue
            columns: dict[str, list[str | None]] = defaultdict(list)
            for record in records:
                for header, value in record.values.items():
                    columns[header].append(value)
            src = records[0].source

            # Profile the whole file first. A column is judged among its siblings:
            # 'Manager ID' is ambiguous alone and obvious beside 'Emp ID'.
            profiles: list[ColumnProfile] = []
            for header, values in columns.items():
                profile = profile_column(
                    SourceRef(file=src.file, sheet=src.sheet, column=header), header, values
                )
                self.profiles[(name, header)] = profile
                profiles.append(profile)

            llm_by_column = self.votes(name, profiles, self.schema) if self.votes else {}

            ranked: list[tuple[str, ColumnMapping]] = []
            for profile in profiles:
                header = profile.raw_name
                mapping = rank_column(
                    profile, self.schema,
                    llm_votes=llm_by_column.get(header) or None,
                    lookups=self.result.lookups,
                )
                ranked.append((header, mapping))

            # One field cannot be fed by two columns of the same file. Across files
            # it can and must - that is how complementary exports merge - but within
            # one file it means the weaker claim belongs somewhere else.
            for header, mapping in _resolve_within_file(ranked):
                self.result.mappings.append(mapping)

                profile = self.profiles[(name, header)]
                forced = self.overrides.column_map.get((name, header))
                if forced and forced not in self.schema.by_name:
                    # Answered against a field this schema no longer has - renamed or
                    # removed since. Applying it would name a field that does not
                    # exist, so the column is judged afresh and the history says why.
                    self._log(
                        "mapping.answer_dropped",
                        f"You mapped {header!r} to {forced}, which this schema no longer "
                        "has, so it was looked at again",
                        actor=Actor.HUMAN, source_refs=[profile.source],
                    )
                    forced = None
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
                    plan = plan_column(profile, spec)
                    chosen = self.overrides.formats.get((name, header))
                    if chosen and plan.date_ambiguous_between:
                        # Answered once for the column; every row reads that way now.
                        plan = ColumnPlan(
                            date_format=chosen,
                            notes=[f"{chosen} because you said so for this column"],
                        )
                    self.plans[(name, header)] = plan
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
        required = {f.name for f in self.schema.required_fields}

        # A file supplying almost none of the required fields is not an employee export
        # that is missing a column — it is a different kind of file. Asking about each
        # missing field separately turns one judgement into a dozen identical prompts.
        unusable = set()
        for origin in files:
            supplied = set(applied.get(origin, {}).values()) & required
            if required and len(supplied) / len(required) < UNRECOGNISED_FILE_RATIO:
                unusable.add(origin)
                self._unrecognised_file(origin, supplied, required)

        for spec in self.schema.required_fields:
            if spec.name in everywhere:
                missing_from = [
                    f for f in files
                    if spec.name not in set(applied.get(f, {}).values())
                    and (f, spec.name) not in self.field_case
                    and f not in unusable
                ]
            elif (None, spec.name) in self.field_case:
                continue
            else:
                missing_from = [None]

            for origin in missing_from:
                if origin is not None and (origin, spec.name) in self.field_case:
                    continue
                if origin in unusable:
                    continue
                self._raise_unmapped(spec, origin)

    def _unrecognised_file(
        self, origin: str, supplied: set[str], required: set[str]
    ) -> None:
        """One case for a file that does not look like the entity at all."""
        missing = sorted(required - supplied)
        case = self._case(
            EscalationClass.UNMAPPED_REQUIRED,
            headline=f"{origin} does not look like {self.schema.entity} data",
            detail=(
                f"Only {len(supplied)} of {len(required)} required fields could be "
                f"matched to a column in this file. It may be a lookup table, an export "
                f"of something else, or the wrong file."
            ),
            source_refs=[SourceRef(file=origin)],
            evidence={
                "matched": sorted(supplied) or ["nothing"],
                "missing": missing[:10],
                # The answer is about this whole file - ignore it or not.
                "whole_file": origin,
            },
            actions=[Action.CORRECT, Action.REJECT],
            options=[Option(label="Ignore this file", value="exclude", recommended=True)],
        )
        # Every required field defers to this one case rather than asking again.
        for name in required:
            self.field_case.setdefault((origin, name), case.id)

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
                # Which file the answer is for, or None for every file. The closest
                # column can sit in another file, so it cannot be the scope.
                "scope": origin,
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
                        row=extracted.source.row, page=extracted.source.page,
                        column=header,
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

        chosen = self.overrides.value_map.get((ref.file, ref.column or "", raw or ""))
        if chosen is not None:
            # Answered once for this value in this column; every row with it reads so.
            record.values[spec.name] = chosen
            record.provenance[spec.name] = FieldProvenance(
                target_field=spec.name, source=ref, raw_value=raw, value=chosen,
                transformations=[
                    Transformation(
                        rule="human_correction", before=raw, after=chosen,
                        reason="chosen by a reviewer for every row with this value",
                    )
                ],
            )
            return

        try:
            value, steps = clean(raw, spec, plan)
        except UnsafeValue as unsafe:
            record.state = RecordState.BLOCKED
            read_confidence = self.confidence.get(record.id, {}).get(ref.column or "", 1.0)
            # Two-sided on purpose. Low confidence alone is not enough — OCR is
            # routinely unsure about text it read correctly — and a bad value alone is
            # not enough either, since the file may simply contain one. Both together
            # say the problem is likely in the reading.
            unreadable = read_confidence < LOW_READ_CONFIDENCE
            klass = (
                EscalationClass.LOW_CONFIDENCE_EXTRACTION if unreadable
                else EscalationClass.AMBIGUOUS_VALUE if unsafe.alternatives
                else EscalationClass.VALIDATION_UNRESOLVED
            )
            headline = (
                f"{spec.name} was hard to read from the scan"
                if unreadable else f"{spec.name} could not be cleaned safely"
            )
            detail = (
                f"Read at {read_confidence:.0%} confidence, and the value does not fit "
                f"{spec.name}. {unsafe.reason}" if unreadable else unsafe.reason
            )
            # A date column that nothing in the data can settle is one fact about the
            # column. Asked per row it produced five identical questions on the messy
            # sample; asked once it produces one, and the answer reads every row.
            ambiguous_column = bool(
                unsafe.alternatives and plan and plan.date_ambiguous_between and ref.column
            )
            if ambiguous_column:
                key = (ref.file, ref.column or "")
                existing = self.format_case.get(key)
                if existing:
                    self._attach_to_case(existing, record, spec.name)
                    self.format_rows[existing].append((record.id, raw or ""))
                    return
                case = self._case(
                    EscalationClass.AMBIGUOUS_VALUE,
                    headline=f"How should dates in {ref.column!r} be read?",
                    detail=(
                        f"{raw!r} could be {' or '.join(unsafe.alternatives)}, and no "
                        f"value anywhere in this column settles it — none has a day "
                        f"above 12. Answer once and every row in the column is read "
                        f"the same way."
                    ),
                    record_key=self._record_key_for(record) or record.id,
                    target_field=spec.name,
                    source_refs=[ref],
                    raw_values=[raw or ""],
                    actions=[Action.CORRECT, Action.REJECT],
                    options=[
                        Option(label=_format_label(fmt, raw or ""), value=fmt)
                        for fmt in plan.date_ambiguous_between
                    ],
                    blocks_records=[record.id],
                )
                self.format_case[key] = case.id
                self.format_rows[case.id].append((record.id, raw or ""))
                record.open_cases.append(case.id)
                self.value_case[(record.id, spec.name)] = case.id
                return

            # A value that fits none of the allowed options is a fact about that value
            # in that column, not about each person holding it. Asked per row, four
            # interns were four identical questions on the clean sample.
            per_value = bool(
                klass is EscalationClass.AMBIGUOUS_VALUE and ref.column and raw is not None
            )
            group = (ref.file, ref.column or "", raw or "")
            if per_value and group in self.value_group:
                self._attach_to_case(self.value_group[group], record, spec.name)
                return
            if per_value:
                case = self._case(
                    klass,
                    headline=f"{raw!r} is not one of the allowed {spec.name} values",
                    detail=(
                        f"{unsafe.reason}. Choose what it should become and every row "
                        f"with {raw!r} in {ref.column!r} is read the same way."
                    ),
                    record_key=self._record_key_for(record) or record.id,
                    target_field=spec.name,
                    source_refs=[ref],
                    raw_values=[raw or ""],
                    rule=", ".join(spec.allowed) if spec.allowed else None,
                    actions=[Action.CORRECT, Action.REJECT],
                    options=[Option(label=alt, value=alt) for alt in unsafe.alternatives],
                    blocks_records=[record.id],
                    evidence={"per_value": True},
                )
                self.value_group[group] = case.id
                record.open_cases.append(case.id)
                self.value_case[(record.id, spec.name)] = case.id
                return

            case = self._case(
                klass,
                headline=headline,
                detail=detail,
                record_key=self._record_key_for(record) or record.id,
                target_field=spec.name,
                source_refs=[ref],
                raw_values=[raw or ""],
                rule=spec.pattern or (", ".join(spec.allowed) if spec.allowed else None),
                actions=[Action.CORRECT, Action.REJECT],
                options=[Option(label=alt, value=alt) for alt in unsafe.alternatives],
                blocks_records=[record.id],
                evidence=(
                    {"read_confidence": round(read_confidence, 3),
                     "crop": {"file": ref.file, "page": ref.page, "column": ref.column,
                              "row": ref.row}}
                    if unreadable else {}
                ),
            )
            record.open_cases.append(case.id)
            # Remember it so validation does not report the same field as simply missing.
            self.value_case[(record.id, spec.name)] = case.id
            return

        record.values[spec.name] = value
        if spec.name == self._identity_field() and value not in (None, ""):
            record.natural_key = str(value)
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

        self.merged_into = merged_into
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
            mine_from = keep.contributing[0].file if keep.contributing else ""
            theirs_from = other.contributing[0].file if other.contributing else ""
            winner = self.overrides.prefer(mine_from, theirs_from)
            if winner:
                # The client has already said which export to believe. Asking again
                # per field, per person, is the thing that makes a queue unusable.
                chosen = mine if winner == mine_from else theirs
                keep.values[name] = chosen
                self._log(
                    "merge.authority",
                    f"{name} taken from {winner} as you set",
                    actor=Actor.HUMAN, target_field=name,
                    before=str(theirs if winner == mine_from else mine),
                    after=str(chosen),
                )
                continue

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
                options=[
                    Option(label=str(mine), value=str(mine),
                           description=f"as {mine_from} has it"),
                    Option(label=str(theirs), value=str(theirs),
                           description=f"as {theirs_from} has it"),
                    # The rule, rather than the answer. One of these ends every
                    # disagreement between these two files at once.
                    Option(label=f"Always believe {mine_from}",
                           value=f"authority:{mine_from}",
                           description="Settles every disagreement this file is part of"),
                    Option(label=f"Always believe {theirs_from}",
                           value=f"authority:{theirs_from}",
                           description="Settles every disagreement this file is part of"),
                ],
                blocks_records=[keep.id],
            )
            keep.open_cases.append(case.id)
            keep.state = RecordState.BLOCKED

    def _cross_check_formats(self, records: list[CanonicalRecord]) -> None:
        """Test each undecidable date column against the same people in other files.

        Nothing inside the column settles 05/07/2018. But if that person also appears
        in another export with an unambiguous 2018-07-05, the two readings are no
        longer equally likely. That is measured evidence, so it earns a
        recommendation - never the answer: a reading that holds for everyone checked
        is suggested, the one nobody matches is cautioned against, and the person
        still decides. Only another file counts; a file cannot vouch for itself.
        """
        survivors = {r.id: r for r in records}
        for case_id, rows in self.format_rows.items():
            case = next((c for c in self.result.cases if c.id == case_id), None)
            spec = self.schema.by_name.get(case.target_field or "") if case else None
            if case is None or spec is None or not case.source_refs:
                continue
            here = case.source_refs[0]
            target = spec.strftime or "%Y-%m-%d"
            readings = [o.value for o in case.options if o.value]
            agree = dict.fromkeys(readings, 0)
            examples: list[dict] = []
            others: list[str] = []

            for record_id, raw in rows:
                record = survivors.get(self._root(record_id, self.merged_into))
                if record is None:
                    continue
                there = record.values.get(spec.name)
                origin = record.provenance.get(spec.name)
                if there in (None, "") or origin is None or origin.source.file == here.file:
                    continue
                matches = [fmt for fmt in readings if _read_as(raw, fmt, target) == str(there)]
                for fmt in matches:
                    agree[fmt] += 1
                if origin.source.file not in others:
                    others.append(origin.source.file)
                examples.append({
                    "key": self._record_key_for(record) or record.id,
                    "here": raw, "there": str(there), "file": origin.source.file,
                    "matches": [_format_lead(fmt) for fmt in matches],
                })

            checked = len(examples)
            if not checked:
                continue
            files = " and ".join(others)
            supported = [fmt for fmt, n in agree.items() if n]
            case.evidence = {**case.evidence, "cross_check": {
                "file": here.file, "column": here.column, "against": others,
                "checked": checked,
                "agree": {_format_lead(fmt): n for fmt, n in agree.items()},
                "examples": examples[:5],
            }}

            def matching(n: int) -> str:
                return f"{n} {'matches' if n == 1 else 'match'}"

            tally = "; ".join(
                f"{matching(n)} {_format_lead(fmt).lower()}" for fmt, n in agree.items() if n
            ) or "none match either reading"
            neither = checked - sum(agree.values())
            if neither and supported:
                tally += f"; {matching(neither)} neither"

            if len(supported) == 1:
                best = supported[0]
                lead = _format_lead(best).lower()
                case.detail += (
                    f" But {checked} of the people waiting on this also appear in {files}, "
                    f"and {agree[best]} of {checked} match {lead}."
                )
                for option in case.options:
                    if option.value == best:
                        option.recommended = True
                        option.description = (
                            f"{agree[best]} of {checked} people who also appear in "
                            f"{files} match this reading"
                        )
                    elif option.value:
                        option.caution = (
                            f"None of the {checked} people who also appear in {files} "
                            f"match this reading. Choosing it gives them a different "
                            f"date from the one {files} has."
                        )
            else:
                # A split is a finding too: the column may not be one format at all.
                case.detail += (
                    f" Checked against {files}: of {checked} people who appear in both, "
                    f"{tally}. That does not settle it."
                )

            self._log(
                "format.cross_checked",
                f"Checked {here.column!r} in {here.file} against {files}: "
                f"of {checked} people in both, {tally}",
                case_id=case.id, target_field=spec.name, source_refs=[here],
            )

    def _validate(self, records: list[CanonicalRecord]) -> None:
        self.result.records = records
        identity = self._identity_field()
        known = {self.schema.entity: {
            str(r.values.get(identity)) for r in records if r.values.get(identity)
        }}
        dupes = check_uniqueness([(r.id, r.values) for r in records], self.schema)

        # Validate everything first, so a record referencing another can be told apart
        # from a record with a problem of its own.
        findings: dict[str, ValidationResult] = {}
        for record in records:
            record.validation_attempts = 1
            result = validate_record(
                record.values, self.schema, lookups=self.result.lookups, known_keys=known
            )
            result.issues.extend(dupes.get(record.id, []))
            record.validation = result
            findings[record.id] = result

        # A record is troubled if it has a problem of its own, or is already held by a
        # case raised earlier (an unclean value, an uncertain identity).
        for record in records:
            result = findings[record.id]
            if result.ok:
                continue

            # One bounded correction cycle, then stop. Repeating an identical failed
            # check is not a second attempt, it is a loop.
            record.validation_attempts = MAX_VALIDATION_ATTEMPTS

            for issue in result.errors:
                if issue.rule == "required" and issue.target_field:
                    if (record.id, issue.target_field) in self.value_case:
                        continue
                    origin = record.contributing[0].file if record.contributing else None
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

    def _hold_dependents(self) -> None:
        """Hold a record whose reference points at one that is not going anywhere.

        A manager reference to a record that exists but is blocked passes validation —
        the record is there. It is delivery that cannot proceed: sending the report
        before the manager leaves a reference dangling at the destination.

        Such a record has nothing of its own to decide, so it rides along on whatever is
        holding the record it depends on, rather than becoming a question nobody can
        answer. Chains are followed until nothing changes.
        """
        identity = self._identity_field()
        by_key = {
            str(r.values.get(identity)): r
            for r in self.result.records if r.values.get(identity)
        }
        self_refs = [
            f for f in self.schema.fields
            if (ref := f.reference_target) and ref[0] == self.schema.entity
        ]
        if not self_refs:
            return

        for _ in range(len(self.result.records)):
            changed = False
            for record in self.result.records:
                if record.state is not RecordState.READY:
                    continue
                for spec in self_refs:
                    value = record.values.get(spec.name)
                    other = by_key.get(str(value)) if value else None
                    if other is None or other.id == record.id:
                        continue
                    if other.state is RecordState.READY:
                        continue
                    for case_id in other.open_cases:
                        case = next(
                            (c for c in self.result.cases if c.id == case_id), None
                        )
                        if case and record.id not in case.child_records:
                            case.child_records.append(record.id)
                        if case_id not in record.open_cases:
                            record.open_cases.append(case_id)
                    if record.open_cases:
                        record.state = RecordState.BLOCKED
                        changed = True
                        self._log(
                            "record.waiting_on_another",
                            f"{record.values.get(identity, record.id)} is waiting for "
                            f"{value} to be resolved first",
                            record_id=record.id,
                            target_field=spec.name,
                            reason=(
                                "sending it now would leave a reference to a record "
                                "the destination does not have"
                            ),
                        )
                    break
            if not changed:
                break

    def _finalise(self) -> None:
        for record in self.result.records:
            key = self._record_key_for(record)
            # Left out by employee key, or by row for a record that has no key yet -
            # which is what a whole-file or whole-column "leave these out" names.
            if (key and key in self.overrides.excluded) or record.id in self.overrides.excluded:
                record.state = RecordState.EXCLUDED
                record.open_cases.clear()
                continue
            if record.open_cases or not record.validation.ok:
                record.state = RecordState.BLOCKED
            else:
                record.state = RecordState.READY
        self._hold_dependents()
        ready = len(self.result.ready)
        self._log(
            "run.processed",
            f"{ready} record(s) ready, {len(self.result.blocked)} awaiting a decision, "
            f"{len(self.result.open_cases)} question(s) raised",
        )


def _format_lead(fmt: str) -> str:
    """'%d/%m/%Y' is 'Day first' to someone who has never seen a format string."""
    return "Day first" if fmt.startswith("%d") else "Month first" if fmt.startswith("%m") \
        else fmt


def _read_as(raw: str, fmt: str, target: str) -> str | None:
    """A value read one particular way, in the field's own format, or None."""
    import datetime as _dt

    try:
        return _dt.datetime.strptime(raw.strip(), fmt).strftime(target)  # noqa: DTZ007
    except (ValueError, TypeError):
        return None


def _format_label(fmt: str, example: str) -> str:
    """'%d/%m/%Y' plus '01/04/2015' reads as 'Day first — 1 April 2015'."""
    import datetime as _dt

    try:
        # A calendar date, never an instant - no timezone applies to "4 April 2015".
        shown = _dt.datetime.strptime(example, fmt).date().strftime("%-d %B %Y")  # noqa: DTZ007
    except (ValueError, TypeError):
        shown = fmt
    return f"{_format_lead(fmt)} — {example} is {shown}"


def apply_decision(
    overrides: Overrides, case: ReviewCase, action: Action, value: str | None
) -> Overrides:
    """Translate one human decision into a re-runnable override.

    Rejection never deletes source data. It marks the affected records excluded so
    they are not delivered, and the decision stays in the audit trail.
    """
    whole_file = case.evidence.get("whole_file") if case.evidence else None
    if whole_file and (action is Action.REJECT or value == "exclude"):
        # "This is not employee data" - so it is not read at all, rather than read and
        # then every row of it held back.
        overrides.ignored_files.add(str(whole_file))
        return overrides

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
                # The question says which file it is about, or none for every file.
                # The closest column's file was used before, which scoped a run-wide
                # answer to one file and a one-file answer to the whole run.
                if case.evidence and "scope" in case.evidence:
                    key_scope = case.evidence["scope"]
                else:
                    key_scope = scope if "has no column" in case.headline and scope else None
                overrides.constants[(key_scope, case.target_field or "")] = constant

        case EscalationClass.UNCERTAIN_IDENTITY:
            keys = tuple(case.evidence.get("keys", ())) if case.evidence else ()
            # Merging two people is only ever what somebody chose. "Same person"
            # arrives as the chosen option; approving means taking the recommendation,
            # which is to keep them apart. The old reading had both backwards.
            chosen = value
            if action is Action.APPROVE and chosen is None:
                chosen = next((o.value for o in case.options if o.recommended), None)
            if len(keys) == 2 and chosen in ("merge", "separate"):
                overrides.merge[(str(keys[0]), str(keys[1]))] = chosen == "merge"

        case EscalationClass.AMBIGUOUS_VALUE if value and case.evidence.get("per_value"):
            # One answer for the value in its column, for every row that holds it.
            ref = case.source_refs[0] if case.source_refs else None
            if ref and ref.column and case.raw_values:
                overrides.value_map[(ref.file, ref.column, case.raw_values[0])] = value

        case EscalationClass.AMBIGUOUS_VALUE if value and value.startswith("%"):
            # A reading chosen for the column, not for the row that happened to ask.
            ref = case.source_refs[0] if case.source_refs else None
            if ref and ref.column:
                overrides.formats[(ref.file, ref.column)] = value

        case EscalationClass.CONFLICTING_FACTS if value and value.startswith("authority:"):
            winner = value.split(":", 1)[1]
            # Most recently trusted goes to the front, so a later rule overrides an
            # earlier one rather than silently losing to it.
            if winner in overrides.authority:
                overrides.authority.remove(winner)
            overrides.authority.insert(0, winner)

        case _ if value and case.record_key and case.target_field:
            overrides.values[(case.record_key, case.target_field)] = value

    return overrides
